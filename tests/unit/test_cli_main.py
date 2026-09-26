"""CLI wiring in `inferrail.cli.main`: `serve --quickstart`, the unchanged
explicit-config path, and `report`'s fallback to the quickstart receipts
path when no `inferrail.yaml` exists.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from inferrail.cli.main import main


@pytest.fixture
def _no_real_server(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Prevent `_cmd_serve` from actually binding a port; record the call."""
    calls: list[dict[str, Any]] = []

    def _fake_run(app: Any, *, host: str, port: int) -> None:
        calls.append({"app": app, "host": host, "port": port})

    monkeypatch.setattr("uvicorn.run", _fake_run)
    return calls


def test_serve_quickstart_needs_no_inferrail_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_server: list[dict[str, Any]],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    result = main(["serve", "--quickstart"])

    assert result == 0
    assert len(_no_real_server) == 1
    assert not (tmp_path / "inferrail.yaml").exists()  # never silently written
    out = capsys.readouterr().out
    assert "quickstart defaults" in out


def test_serve_quickstart_starts_without_the_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_server: list[dict[str, Any]],
) -> None:
    # The server (and /health) must come up even before OPENAI_API_KEY is
    # configured — a missing key only matters once a request actually
    # reaches that provider (providers/openai.py's OpenAIProvider.complete),
    # not at startup. See providers/registry.py's require_keys parameter.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = main(["serve", "--quickstart"])

    assert result == 0
    assert len(_no_real_server) == 1
    assert capsys.readouterr().err == ""


def test_serve_without_quickstart_flag_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_server: list[dict[str, Any]],
) -> None:
    monkeypatch.chdir(tmp_path)

    result = main(["serve"])

    assert result == 1
    assert _no_real_server == []
    err = capsys.readouterr().err
    assert "config file not found" in err


def test_serve_explicit_config_path_still_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_real_server: list[dict[str, Any]],
) -> None:
    config_path = tmp_path / "inferrail.yaml"
    config_path.write_text(
        """
providers:
  openai:
    type: openai
    api_key_env: TEST_SERVE_KEY
routes:
  default:
    provider: openai
    model: gpt-4o-mini
telemetry:
  sink: none
receipts:
  sink: none
"""
    )
    monkeypatch.setenv("TEST_SERVE_KEY", "sk-test-not-a-real-key")

    result = main(["serve", "--config", str(config_path)])

    assert result == 0
    assert len(_no_real_server) == 1


def _write_serve_config(path: Path, *, env_var: str = "TEST_SERVE_KEY") -> Path:
    config_path = path / "inferrail.yaml"
    config_path.write_text(
        f"""
providers:
  openai:
    type: openai
    api_key_env: {env_var}
routes:
  default:
    provider: openai
    model: gpt-4o-mini
telemetry:
  sink: none
receipts:
  sink: none
"""
    )
    return config_path


def test_serve_app_mode_relocates_receipts_and_budgets_and_prints_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_server: list[dict[str, Any]],
) -> None:
    config_path = _write_serve_config(tmp_path)
    monkeypatch.setenv("TEST_SERVE_KEY", "sk-test-not-a-real-key")
    app_data = tmp_path / "appdata"
    monkeypatch.setenv("XDG_DATA_HOME", str(app_data))
    monkeypatch.setattr("sys.platform", "linux")

    result = main(["serve", "--config", str(config_path), "--app-mode"])

    assert result == 0
    assert len(_no_real_server) == 1
    app = _no_real_server[0]["app"]
    assert (app_data / "inferrail" / "receipts.db").exists()
    assert (app_data / "inferrail" / "local-api-token").exists()
    assert (app_data / "inferrail" / "ap-recovery.db").exists()
    out = capsys.readouterr().out
    assert "App-mode data directory" in out
    assert app.state.local_api_token in out
    assert str(app_data / "inferrail" / "ap-recovery.db") in out


def test_serve_app_mode_honors_inferrail_ap_db_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_server: list[dict[str, Any]],
) -> None:
    config_path = _write_serve_config(tmp_path)
    monkeypatch.setenv("TEST_SERVE_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "appdata"))
    monkeypatch.setattr("sys.platform", "linux")
    override_path = tmp_path / "my-existing-ap-recovery.db"
    monkeypatch.setenv("INFERRAIL_AP_DB", str(override_path))

    result = main(["serve", "--config", str(config_path), "--app-mode"])

    assert result == 0
    assert override_path.exists()
    app = _no_real_server[0]["app"]
    assert app.state.ap_recovery_store.db_path == override_path


def test_serve_quickstart_and_app_mode_combine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_server: list[dict[str, Any]],
) -> None:
    # Providers/routes come from quickstart (no inferrail.yaml needed);
    # receipts/budgets/dashboard/local API still relocate under app-data,
    # exactly like plain --app-mode does — the two are independent axes,
    # not mutually exclusive (see
    # docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    app_data = tmp_path / "appdata"
    monkeypatch.setenv("XDG_DATA_HOME", str(app_data))
    monkeypatch.setattr("sys.platform", "linux")

    result = main(["serve", "--quickstart", "--app-mode"])

    assert result == 0
    assert len(_no_real_server) == 1
    app = _no_real_server[0]["app"]
    assert (app_data / "inferrail" / "receipts.db").exists()
    assert (app_data / "inferrail" / "local-api-token").exists()
    out = capsys.readouterr().out
    assert "App-mode data directory" in out
    assert "OpenAI SDK" in out and "Anthropic SDK" in out  # quickstart banner still printed
    assert app.state.local_api_token in out


def test_serve_quickstart_banner_prints_both_sdk_base_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_server: list[dict[str, Any]],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    result = main(["serve", "--quickstart", "--port", "9999"])

    assert result == 0
    out = capsys.readouterr().out
    assert 'OpenAI(base_url="http://127.0.0.1:9999/v1")' in out
    # No /v1: the Anthropic SDK appends /v1/messages itself.
    assert 'Anthropic(base_url="http://127.0.0.1:9999")' in out
    assert "OPENAI_BASE_URL=http://127.0.0.1:9999/v1" in out
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:9999\n" in out
    assert "verify-payload-free" in out


def test_serve_quickstart_daily_budget_creates_a_block_mode_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_server: list[dict[str, Any]],
) -> None:
    from inferrail.budgets.store import BudgetStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    result = main(["serve", "--quickstart", "--daily-budget-usd", "5.00"])

    assert result == 0
    store = BudgetStore(tmp_path / "inferrail-budgets.db")
    budgets = store.list()
    assert len(budgets) == 1
    assert budgets[0].scope == "global"
    assert budgets[0].window == "daily"
    assert budgets[0].mode == "block"
    assert str(budgets[0].limit_usd) == "5.00"
    out = capsys.readouterr().out
    assert "daily budget: $5.00" in out
    assert "inferrail-receipts.db" in out  # sqlite switch, since --app-mode wasn't given


def test_serve_quickstart_daily_budget_rejects_non_numeric_amount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _no_real_server: list[dict[str, Any]],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    result = main(["serve", "--quickstart", "--daily-budget-usd", "not-a-number"])

    assert result == 1
    assert _no_real_server == []
    assert "not a valid number" in capsys.readouterr().err


def test_serve_no_telemetry_sets_the_environment_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_real_server: list[dict[str, Any]],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.delenv("INFERRAIL_TELEMETRY", raising=False)

    result = main(["serve", "--quickstart", "--no-telemetry"])

    assert result == 0
    assert os.environ.get("INFERRAIL_TELEMETRY") == "0"


def test_report_falls_back_to_quickstart_receipts_path_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    receipts_path = tmp_path / "inferrail-receipts.jsonl"
    receipts_path.write_text(
        '{"receipt_id":"ir_1","request_id":"req_1","route":"default","provider":"openai",'
        '"model":"gpt-4o-mini","status":"success","attributes":{"customer":"acme"},'
        '"total_latency_ms":1.0}\n'
    )

    result = main(["report", "--by", "customer"])

    assert result == 0
    assert "acme" in capsys.readouterr().out


def test_report_without_by_prints_all_up_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inferrail-receipts.jsonl").write_text(
        '{"receipt_id":"ir_1","request_id":"req_1","route":"default",'
        '"provider":"openai","model":"gpt-4o-mini","status":"success",'
        '"prompt_tokens":10,"completion_tokens":5,"attributes":{},"total_latency_ms":1.0}\n'
    )

    result = main(["report"])

    assert result == 0
    output = capsys.readouterr().out
    assert "INFERRAIL SPEND SUMMARY" in output
    assert "Requests:        1" in output


def test_report_by_customer_keeps_grouped_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inferrail-receipts.jsonl").write_text(
        '{"receipt_id":"ir_1","request_id":"req_1","route":"default",'
        '"provider":"openai","model":"gpt-4o-mini","status":"success",'
        '"attributes":{"customer":"acme"},"total_latency_ms":1.0}\n'
    )

    result = main(["report", "--by", "customer"])

    assert result == 0
    output = capsys.readouterr().out
    assert "CUSTOMER" in output
    assert "acme" in output


def test_report_uses_configured_receipts_path_when_config_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    custom_receipts = tmp_path / "custom-receipts.jsonl"
    custom_receipts.write_text(
        '{"receipt_id":"ir_1","request_id":"req_1","route":"default","provider":"openai",'
        '"model":"gpt-4o-mini","status":"success","attributes":{"customer":"globex"},'
        '"total_latency_ms":1.0}\n'
    )
    (tmp_path / "inferrail.yaml").write_text(
        f"""
providers:
  openai:
    type: openai
    api_key_env: TEST_REPORT_KEY
routes:
  default:
    provider: openai
    model: gpt-4o-mini
receipts:
  sink: jsonl
  path: {custom_receipts}
"""
    )
    # Deliberately not set on disk anywhere the quickstart default lives, to
    # prove the config's `receipts.path` was actually used, not a fallback.
    default_path = tmp_path / "inferrail-receipts.jsonl"
    assert not default_path.exists()

    result = main(["report", "--by", "customer"])

    assert result == 0
    assert "globex" in capsys.readouterr().out


def test_report_explicit_config_that_exists_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    custom_receipts = tmp_path / "custom-receipts.jsonl"
    custom_receipts.write_text(
        '{"receipt_id":"ir_1","request_id":"req_1","route":"default","provider":"openai",'
        '"model":"gpt-4o-mini","status":"success","attributes":{"customer":"initech"},'
        '"total_latency_ms":1.0}\n'
    )
    # Named differently from "inferrail.yaml" so this only passes if the
    # *explicit* --config path is actually read, not the implicit default.
    explicit_config_path = tmp_path / "my-config.yaml"
    explicit_config_path.write_text(
        f"""
providers:
  openai:
    type: openai
    api_key_env: TEST_REPORT_KEY
routes:
  default:
    provider: openai
    model: gpt-4o-mini
receipts:
  sink: jsonl
  path: {custom_receipts}
"""
    )

    result = main(["report", "--by", "customer", "--config", str(explicit_config_path)])

    assert result == 0
    assert "initech" in capsys.readouterr().out


def test_transaction_falls_back_to_quickstart_receipts_path_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    receipts_path = tmp_path / "inferrail-receipts.jsonl"
    receipts_path.write_text(
        '{"receipt_id":"ir_1","request_id":"req_1","route":"default","provider":"openai",'
        '"model":"gpt-4o-mini","status":"success","attributes":{"task_id":"bug_9281"},'
        '"total_latency_ms":1.0}\n'
    )

    result = main(["transaction", "bug_9281"])

    assert result == 0
    assert "bug_9281" in capsys.readouterr().out


def test_transaction_explicit_missing_config_errors_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inferrail-receipts.jsonl").write_text(
        '{"receipt_id":"ir_1","request_id":"req_1","route":"default","provider":"openai",'
        '"model":"gpt-4o-mini","status":"success","attributes":{"task_id":"should-not-appear"},'
        '"total_latency_ms":1.0}\n'
    )

    result = main(["transaction", "should-not-appear", "--config", "typo-config.yaml"])

    assert result == 1
    captured = capsys.readouterr()
    assert "config file not found" in captured.err
    assert "should-not-appear" not in captured.out


def test_report_explicit_missing_config_errors_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    # A real quickstart-default receipts file sits right there — if the
    # explicit --config typo silently fell back to it, this receipt's data
    # would leak into the output. It must not.
    (tmp_path / "inferrail-receipts.jsonl").write_text(
        '{"receipt_id":"ir_1","request_id":"req_1","route":"default","provider":"openai",'
        '"model":"gpt-4o-mini","status":"success","attributes":{"customer":"should-not-appear"},'
        '"total_latency_ms":1.0}\n'
    )

    result = main(["report", "--by", "customer", "--config", "typo-config.yaml"])

    assert result == 1
    captured = capsys.readouterr()
    assert "config file not found" in captured.err
    assert "should-not-appear" not in captured.out
    assert "should-not-appear" not in captured.err
