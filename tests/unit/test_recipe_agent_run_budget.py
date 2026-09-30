"""Keeps docs/recipes/agent-run-budget.md honest: runs
examples/agent_run_budget.py (the code the recipe shows) against the real
gateway app with a fake provider, and checks the recipe's claims — one
run id, a declared budget, parallel calls, refusals before the provider,
and a final per-run cost."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

# The example is client code built on the `openai` SDK, which the main CI
# job doesn't install; the ap-exceptions job (which does) runs this file.
pytest.importorskip("openai")

from inferrail.budgets.store import BudgetStore
from inferrail.config.models import InferrailConfig
from inferrail.gateway import app as app_module
from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse
from inferrail.receipts.sqlite_store import ReceiptsStore

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "agent_run_budget.py"


def _load_example():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("agent_run_budget", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses in the module need this
    spec.loader.exec_module(module)
    return module


class _SlowProvider:
    """Answers after a short delay so all of the run's calls overlap."""

    name = "openai"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        self.calls += 1
        await asyncio.sleep(0.05)
        return NormalizedChatResponse(
            content="ok", finish_reason="stop", prompt_tokens=20, completion_tokens=30
        )


@pytest.fixture(autouse=True)
def _no_gateway_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


async def test_recipe_example_does_what_the_recipe_says(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = _SlowProvider()
    config = InferrailConfig.model_validate(
        {
            "providers": {"openai": {"type": "openai", "api_key_env": "OPENAI_API_KEY"}},
            "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
            "default_provider": "openai",
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
            "budgets": {"enabled": True, "path": str(tmp_path / "budgets.db")},
        }
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    app = app_module.create_app(config)
    example = _load_example()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as http_client:
        # Each call reserves ~$0.00012 (gpt-4o-mini, max_tokens=200): room for 4.
        result = await example.run_agent_step(
            base_url="http://gateway/v1", budget_usd="0.0005", parallel_calls=8,
            http_client=http_client,
        )

    assert (result.answered, result.refused_by_budget) == (4, 4)
    assert provider.calls == 4  # refused calls never reached the provider
    [budget] = BudgetStore(tmp_path / "budgets.db").list()  # created by the header
    assert budget.budget_id == f"work_id:{result.run_id}:per_work"
    run = ReceiptsStore(tmp_path / "receipts.db").query(work_id=result.run_id)
    run_cost = sum((r.estimated_cost_usd for r in run if r.estimated_cost_usd), Decimal(0))
    assert run_cost > 0  # the final per-run cost the recipe shows via `inferrail work`
    assert sum(1 for r in run if "budget_id" in r.attributes) == 4
