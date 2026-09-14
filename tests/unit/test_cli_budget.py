"""`inferrail budget set|list|rm` (see docs/adr/0015-budget-enforcement.md)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from inferrail.budgets.store import BudgetStore
from inferrail.cli.budget import run_budget_list, run_budget_rm, run_budget_set
from inferrail.cli.main import main


def test_run_budget_set_creates_a_new_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "budgets.db"

    result = run_budget_set(
        db_path, scope="global", scope_value=None, window="daily", mode="block",
        limit_usd="0.01",
    )

    assert result == 0
    assert "Created budget 'global:_:daily'" in capsys.readouterr().out
    budgets = BudgetStore(db_path).list()
    assert len(budgets) == 1
    assert budgets[0].limit_usd == Decimal("0.01")


def test_run_budget_set_twice_is_an_upsert_not_a_duplicate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "budgets.db"
    run_budget_set(
        db_path, scope="global", scope_value=None, window="daily", mode="warn",
        limit_usd="10",
    )
    capsys.readouterr()

    result = run_budget_set(
        db_path, scope="global", scope_value=None, window="daily", mode="block",
        limit_usd="20",
    )

    assert result == 0
    assert "Updated budget" in capsys.readouterr().out
    budgets = BudgetStore(db_path).list()
    assert len(budgets) == 1
    assert budgets[0].mode == "block"
    assert budgets[0].limit_usd == Decimal("20")


def test_run_budget_set_rejects_invalid_scope_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run_budget_set(
        tmp_path / "budgets.db", scope="project", scope_value=None, window="monthly",
        mode="block", limit_usd="1",
    )

    assert result == 1
    assert "error" in capsys.readouterr().out


def test_run_budget_set_rejects_non_numeric_limit(tmp_path: Path) -> None:
    result = run_budget_set(
        tmp_path / "budgets.db", scope="global", scope_value=None, window="daily",
        mode="block", limit_usd="not-a-number",
    )
    assert result == 1


def test_run_budget_list_empty_store_prints_friendly_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run_budget_list(tmp_path / "does-not-exist.db", as_json=False)

    assert result == 0
    assert "No budget store found" in capsys.readouterr().out


def test_run_budget_list_as_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "budgets.db"
    run_budget_set(
        db_path, scope="global", scope_value=None, window="daily", mode="block",
        limit_usd="0.01",
    )
    capsys.readouterr()

    result = run_budget_list(db_path, as_json=True)

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["budget_id"] == "global:_:daily"


def test_run_budget_rm_removes_an_existing_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "budgets.db"
    run_budget_set(
        db_path, scope="global", scope_value=None, window="daily", mode="block",
        limit_usd="0.01",
    )
    capsys.readouterr()

    result = run_budget_rm(db_path, "global:_:daily")

    assert result == 0
    assert "Removed budget" in capsys.readouterr().out
    assert BudgetStore(db_path).list() == []


def test_run_budget_rm_missing_budget_is_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "budgets.db"
    run_budget_set(
        db_path, scope="global", scope_value=None, window="daily", mode="block",
        limit_usd="0.01",
    )
    capsys.readouterr()

    result = run_budget_rm(db_path, "not-a-real-id")

    assert result == 1
    assert "No budget 'not-a-real-id' found" in capsys.readouterr().out


def test_cli_budget_set_list_rm_round_trip_via_main(tmp_path: Path) -> None:
    db_path = tmp_path / "budgets.db"

    assert main(
        [
            "budget", "set", "--scope", "project", "--scope-value", "acme",
            "--window", "monthly", "--mode", "warn", "--limit-usd", "500",
            "--db", str(db_path),
        ]
    ) == 0
    assert main(["budget", "list", "--db", str(db_path)]) == 0
    assert main(["budget", "rm", "project:acme:monthly", "--db", str(db_path)]) == 0
    assert BudgetStore(db_path).list() == []
