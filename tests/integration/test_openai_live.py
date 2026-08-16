"""Manual integration test: makes one real call to OpenAI.

Skipped unless OPENAI_API_KEY is present in the environment. Never run as
part of the default automated test suite / CI — see docs/ARCHITECTURE.md
and tests/README expectations in docs/PRODUCT.md. Run explicitly with:

    OPENAI_API_KEY=<your-openai-api-key> pytest tests/integration -m integration
"""

from __future__ import annotations

import os

import pytest

from inferrail.providers.base import ChatMessage, NormalizedChatRequest
from inferrail.providers.openai import OpenAIProvider

pytestmark = pytest.mark.integration

requires_real_credentials = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set; skipping real-provider integration test",
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
