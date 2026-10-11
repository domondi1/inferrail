"""The published customer-journey example runs end to end with the real OpenAI and Anthropic
SDKs (stand-in providers): both SDKs authenticate with the workspace key as `api_key`, runs
are receipted and metered, and an over-ceiling call is refused before the provider and not
billed."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("openai")
pytest.importorskip("anthropic")
pytest.importorskip("uvicorn")

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "hosted_quickstart" / "journey.py"


def test_the_customer_journey_runs_with_both_sdks(capsys):
    spec = importlib.util.spec_from_file_location("hosted_journey", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hosted_journey"] = module
    spec.loader.exec_module(module)

    out = module.main([])

    assert out["provider"] == "mock provider" and out["pricing_ok"]
    assert set(out["receipts"]) == {"openai", "anthropic"}
    assert all(r["calls"] == 1 for r in out["receipts"].values())
    assert out["governed_runs"] == 2
    assert out["refused"] is True and out["refusal_billed"] is False
    printed = capsys.readouterr().out
    assert "[mock provider]" in printed and "[live provider]" not in printed
    assert "/llms.txt" in printed
