"""The published multi-agent example (examples/hosted_shared_budget) runs as documented and proves
what it claims: independent processes using the OpenAI SDK share one run ceiling that holds under
concurrency and survives a restart."""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("openai")
pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "hosted_shared_budget"


def test_shared_budget_example_holds_the_ceiling_across_processes_and_a_restart(capsys):
    sys.path.insert(0, str(EXAMPLE))  # spawned agent processes import the module by name
    spec = importlib.util.spec_from_file_location("shared_budget", EXAMPLE / "shared_budget.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["shared_budget"] = module
    spec.loader.exec_module(module)

    s = module.main(["--agents", "3", "--calls-per-agent", "12", "--budget", "0.002"])

    assert s["other_errors"] == 0
    assert s["answered"] > 0 and s["refused"] > 0  # the ceiling was reached and enforced
    assert s["answered"] + s["refused"] == 36 == s["receipts"]
    assert Decimal(s["spent_usd"]) <= Decimal("0.002")
    assert s["within_budget"] is True
    after = s["after_restart"]
    assert after["spent_usd"] == s["spent_usd"] and after["receipts"] == s["receipts"]
    assert after["new_call_refused"] is True
    assert "within the ceiling" in capsys.readouterr().out
