"""Regression guard for examples/economic-receipts/run_demo.py.

Not a test of the example's output formatting — just that it keeps running
against the current `InferenceEngine`/receipt/report API. Nothing else in
CI would catch that script silently rotting if those signatures change,
and a broken zero-API-key demo is exactly the kind of first-impression
failure this project can't afford.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_DEMO_SCRIPT = (
    Path(__file__).parents[2] / "examples" / "economic-receipts" / "run_demo.py"
)


def test_demo_script_runs_and_reports_a_customer_table() -> None:
    result = subprocess.run(
        [sys.executable, str(_DEMO_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    receipts_path = _DEMO_SCRIPT.parent / "demo-receipts.jsonl"
    try:
        assert result.returncode == 0, result.stderr
        assert "acme" in result.stdout
        assert "UNKNOWN COST" in result.stdout
        assert receipts_path.exists()
    finally:
        receipts_path.unlink(missing_ok=True)
