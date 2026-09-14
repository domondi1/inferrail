"""`inferrail pricing update` (see cli/pricing.py) — never fetches over
the network; reports built-in catalog freshness and the real fix."""

from __future__ import annotations

from datetime import date

import pytest

from inferrail.cli.main import main
from inferrail.cli.pricing import STALE_AFTER_DAYS, catalog_freshness, run_pricing_update


def test_catalog_freshness_reports_every_built_in_catalog() -> None:
    results = catalog_freshness(today=date(2026, 10, 1))  # after every catalog's verified_date

    names = {name for name, *_ in results}
    assert names == {"OpenAI", "Anthropic"}
    for _name, count, oldest, age_days, _is_stale in results:
        assert count > 0
        assert oldest is not None
        assert age_days is not None
        assert age_days >= 0


def test_catalog_freshness_flags_stale_entries_far_in_the_future() -> None:
    far_future = date(2030, 1, 1)

    results = catalog_freshness(today=far_future)

    assert all(is_stale for *_rest, is_stale in results)
    assert all(age_days > STALE_AFTER_DAYS for *_rest, age_days, _is_stale in results)


def test_catalog_freshness_not_stale_on_verification_day() -> None:
    results = catalog_freshness(today=date(2026, 9, 14))  # Anthropic's own verified_date

    anthropic = next(r for r in results if r[0] == "Anthropic")
    assert anthropic[4] is False  # is_stale


def test_run_pricing_update_never_makes_a_network_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import socket

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("run_pricing_update must never touch the network")

    monkeypatch.setattr(socket, "socket", _fail_if_called)
    monkeypatch.setattr(socket, "create_connection", _fail_if_called)

    result = run_pricing_update()

    out = capsys.readouterr().out
    assert "never fetches pricing over the network" in out
    assert "pip install --upgrade inferrail" in out
    assert result in (0, 1)


def test_cli_pricing_update_via_main(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["pricing", "update"])

    assert result in (0, 1)
    assert "OpenAI" in capsys.readouterr().out
