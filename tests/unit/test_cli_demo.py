"""`inferrail demo`: offline, zero-key, real receipt/report machinery.

Two layers: a fast direct call to `run_demo()` covering the actual
behavior, and a subprocess-level guard (mirroring the old
examples/economic-receipts regression test) that exercises the exact
`inferrail` entry point a user types, so a signature drift anywhere in the
chain can't silently break the zero-API-key first impression without a
test noticing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from inferrail.cli.demo import RECEIPTS_PATH, run_demo


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_demo_runs_offline_with_no_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No OPENAI_API_KEY, no network — the demo must not need either.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = run_demo()

    assert result == 0


def test_demo_output_is_unmistakably_labeled(capsys: pytest.CaptureFixture[str]) -> None:
    run_demo()

    out = capsys.readouterr().out
    assert "DEMO" in out
    assert "not real provider billing" in out.lower()


def test_demo_writes_real_receipts_and_prints_a_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_demo()

    out = capsys.readouterr().out
    assert "CUSTOMER" in out  # inferrail report --by customer ran for real
    assert "UNKNOWN COST" in out  # the unpriced demo-preview model, honestly labeled
    assert RECEIPTS_PATH.exists()


def test_demo_receipts_use_real_receipt_schema_and_demo_labeled_pricing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from inferrail.cli.report import load_receipts

    run_demo()
    capsys.readouterr()

    receipts, skipped = load_receipts(RECEIPTS_PATH)

    assert skipped == 0
    assert len(receipts) == 6
    priced = [r for r in receipts if r.pricing is not None]
    assert priced  # at least one receipt resolved a price
    for receipt in priced:
        # Never mistaken for a real, verified provider price.
        assert "DEMO" in receipt.pricing.source  # type: ignore[union-attr]
    unpriced = [r for r in receipts if r.pricing is None]
    assert len(unpriced) == 1  # the demo-preview scenario, deliberately unpriced
    assert unpriced[0].estimated_cost_usd is None


def test_demo_is_reproducible_across_runs(capsys: pytest.CaptureFixture[str]) -> None:
    run_demo()
    capsys.readouterr()
    run_demo()  # must not fail on a leftover receipts file from the first run
    out = capsys.readouterr().out

    assert "CUSTOMER" in out


def test_inferrail_demo_entry_point_does_not_silently_rot() -> None:
    inferrail_bin = shutil.which("inferrail")
    command = (
        [inferrail_bin, "demo"]
        if inferrail_bin
        else [sys.executable, "-m", "inferrail.cli.main", "demo"]
    )

    result = subprocess.run(command, capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "acme" in result.stdout
    assert "UNKNOWN COST" in result.stdout
