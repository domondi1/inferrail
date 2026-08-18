"""CLI wiring in `inferrail.cli.main`: `serve --quickstart`, the unchanged
explicit-config path, and `report`'s fallback to the quickstart receipts
path when no `inferrail.yaml` exists.
"""

from __future__ import annotations

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
