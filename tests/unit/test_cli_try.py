"""`inferrail try`: one real request through the actual `InferenceEngine`,
exercised here against a fake provider (no network) so the full internal
execution path — quickstart config, engine wiring, receipt persistence,
CLI output — is covered without a real API key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from inferrail.cli import try_cmd
from inferrail.cli.try_cmd import run_try
from inferrail.errors import AuthenticationError, InvalidRequestError
from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse


class _FakeProvider:
    name = "openai"

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self._outcomes = outcomes or [
            NormalizedChatResponse(
                content="ready", finish_reason="stop", prompt_tokens=18, completion_tokens=7
            )
        ]
        self.calls: list[NormalizedChatRequest] = []

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        self.calls.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Any] | None = None
) -> _FakeProvider:
    provider = _FakeProvider(outcomes)
    monkeypatch.setattr(try_cmd, "build_providers", lambda config: {"openai": provider})
    return provider


def test_missing_api_key_shows_guidance_and_suggests_demo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = run_try("hello", customer=None, workflow=None, attribute_args=[], model="gpt-4o-mini")

    assert result == 1
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY is not set" in err
    assert "inferrail demo" in err


def test_successful_try_shows_response_and_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_provider(monkeypatch)

    result = run_try(
        "Reply with one word: ready",
        customer=None,
        workflow=None,
        attribute_args=[],
        model="gpt-4o-mini",
    )

    assert result == 0
    out = capsys.readouterr().out
    assert "ready" in out
    assert "Receipt ir_" in out
    assert "Input tokens      18" in out
    assert "Output tokens     7" in out
    assert "Cost              $0.000007" in out
    assert "Prompt stored     no" in out
    assert "Response stored   no" in out
    assert "inferrail report --by provider" in out


def test_receipt_is_actually_persisted_to_jsonl(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _patch_provider(monkeypatch)

    run_try("hello", customer=None, workflow=None, attribute_args=[], model="gpt-4o-mini")
    capsys.readouterr()

    receipts_path = tmp_path / "inferrail-receipts.jsonl"
    assert receipts_path.exists()
    raw = receipts_path.read_text()
    assert '"prompt_tokens":18' in raw
    assert '"estimated_cost_usd":"0.000007"' in raw


def test_no_prompt_or_response_content_is_persisted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    secret_prompt = "MARKER-PROMPT-9f3a1c-do-not-persist"
    secret_response = "MARKER-RESPONSE-9f3a1c-do-not-persist"
    _patch_provider(
        monkeypatch,
        outcomes=[
            NormalizedChatResponse(
                content=secret_response, finish_reason="stop", prompt_tokens=5, completion_tokens=5
            )
        ],
    )

    run_try(secret_prompt, customer=None, workflow=None, attribute_args=[], model="gpt-4o-mini")
    capsys.readouterr()

    raw = (tmp_path / "inferrail-receipts.jsonl").read_text()
    assert secret_prompt not in raw
    assert secret_response not in raw


def test_customer_and_workflow_convenience_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _patch_provider(monkeypatch)

    run_try(
        "hello", customer="globex", workflow="research", attribute_args=[], model="gpt-4o-mini"
    )
    out = capsys.readouterr().out

    assert "Customer          globex" in out
    assert "Workflow          research" in out
    assert "inferrail report --by customer" in out
    raw = (tmp_path / "inferrail-receipts.jsonl").read_text()
    assert '"customer":"globex"' in raw
    assert '"workflow":"research"' in raw


def test_generic_attribute_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _patch_provider(monkeypatch)

    run_try(
        "hello",
        customer=None,
        workflow=None,
        attribute_args=["environment=prod"],
        model="gpt-4o-mini",
    )
    capsys.readouterr()

    raw = (tmp_path / "inferrail-receipts.jsonl").read_text()
    assert '"environment":"prod"' in raw


def test_malformed_attribute_is_rejected_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = _patch_provider(monkeypatch)

    result = run_try(
        "hello", customer=None, workflow=None, attribute_args=["badformat"], model="gpt-4o-mini"
    )

    assert result == 1
    assert "invalid --attribute" in capsys.readouterr().err
    assert provider.calls == []


def test_duplicate_attribute_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_provider(monkeypatch)

    result = run_try(
        "hello",
        customer="acme",
        workflow=None,
        attribute_args=["customer=other"],
        model="gpt-4o-mini",
    )

    assert result == 1
    assert "duplicate attribute 'customer'" in capsys.readouterr().err


def test_authentication_error_gives_actionable_guidance_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_provider(
        monkeypatch, outcomes=[AuthenticationError("bad key", provider="openai", status_code=401)]
    )

    result = run_try("hello", customer=None, workflow=None, attribute_args=[], model="gpt-4o-mini")

    assert result == 1
    err = capsys.readouterr().err
    assert "OpenAI rejected the API key" in err
    assert "Traceback" not in err


def test_invalid_request_error_names_the_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_provider(
        monkeypatch,
        outcomes=[
            InvalidRequestError("bad model", provider="openai", status_code=404)
        ],
    )

    result = run_try(
        "hello", customer=None, workflow=None, attribute_args=[], model="totally-not-a-model"
    )

    assert result == 1
    err = capsys.readouterr().err
    assert "totally-not-a-model" in err


def test_unrecognized_model_leaves_cost_unknown_not_fabricated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_provider(
        monkeypatch,
        outcomes=[
            NormalizedChatResponse(
                content="ok", finish_reason="stop", prompt_tokens=10, completion_tokens=5
            )
        ],
    )

    result = run_try(
        "hello", customer=None, workflow=None, attribute_args=[], model="some-unpriced-model"
    )

    assert result == 0
    out = capsys.readouterr().out
    assert "unknown (no verified price on file for this model)" in out
    assert "$0.00" not in out


def test_unwritable_receipts_path_fails_beautifully(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_provider(monkeypatch)
    monkeypatch.setattr(
        try_cmd,
        "build_receipt_sink",
        lambda config: (_ for _ in ()).throw(OSError("Permission denied")),
    )

    result = run_try("hello", customer=None, workflow=None, attribute_args=[], model="gpt-4o-mini")

    assert result == 1
    err = capsys.readouterr().err
    assert "could not prepare receipts file" in err
    assert "Traceback" not in err
