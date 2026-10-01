"""`inferrail.start()` runs the same gateway app in-process. These tests
drive it over real HTTP (a fake provider on its own local server) and
check that admission, refusals, streaming, receipts and header handling
are exactly the gateway's, plus the start/stop lifecycle and the
zero-config receipts lookup for `inferrail work`."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import inferrail
import inferrail.cli.main  # noqa: F401  (registers the module)
from inferrail import embedded
from inferrail.config.models import InferrailConfig
from inferrail.gateway import app as app_module
from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse
from inferrail.receipts.sqlite_store import ReceiptsStore

cli_main = sys.modules["inferrail.cli.main"]  # `inferrail.cli.main` the attribute is the function

_USAGE = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
# gpt-4o-mini at OpenAI list price: 100 * 0.15/M + 50 * 0.60/M
_CALL_COST = Decimal("0.000045")


class _FakeUpstream:
    """An OpenAI-compatible provider on a real local port."""

    def __init__(self) -> None:
        self.calls = 0
        self.inferrail_headers_seen = 0
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat(req: Request) -> Any:
            body = await req.json()
            self.calls += 1
            if any(k.lower().startswith("x-inferrail") for k in req.headers):
                self.inferrail_headers_seen += 1
            await asyncio.sleep(0.05)
            base = {"id": "chatcmpl-1", "created": 0, "model": body["model"]}
            if not body.get("stream"):
                return JSONResponse(
                    {
                        **base,
                        "object": "chat.completion",
                        "usage": _USAGE,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "ok"},
                            }
                        ],
                    }
                )

            def chunks() -> Iterator[str]:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            **base,
                            "object": "chat.completion.chunk",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": "ok"},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            **base,
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        }
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {**base, "object": "chat.completion.chunk", "choices": [], "usage": _USAGE}
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"

            return StreamingResponse(chunks(), media_type="text/event-stream")

        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture
def upstream() -> Iterator[_FakeUpstream]:
    fake = _FakeUpstream()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("UPSTREAM_KEY", "test")
    inferrail.stop()
    yield
    inferrail.stop()


def _config(tmp_path: Path, upstream: _FakeUpstream) -> InferrailConfig:
    return InferrailConfig.model_validate(
        {
            "providers": {
                "up": {
                    "type": "openai_compatible",
                    "api_key_env": "UPSTREAM_KEY",
                    "base_url": f"http://127.0.0.1:{upstream.port}/v1",
                    "price_as": "openai",
                    "request_stream_usage": True,
                }
            },
            "routes": {"default": {"provider": "up", "model": "gpt-4o-mini"}},
            "default_provider": "up",
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
            "budgets": {"enabled": True, "path": str(tmp_path / "budgets.db")},
        }
    )


def _headers(run_id: str, budget: str) -> dict[str, str]:
    return {"X-Inferrail-Attribute-Work-Id": run_id, "X-Inferrail-Budget-Usd": budget}


def _chat(**extra: Any) -> dict[str, Any]:
    return {
        "model": "gpt-4o-mini",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    }


def test_start_returns_a_v1_url_on_a_free_port(tmp_path: Path, upstream: _FakeUpstream) -> None:
    config = _config(tmp_path, upstream)
    url = inferrail.start(config)
    assert url.startswith("http://127.0.0.1:") and url.endswith("/v1")
    assert int(url.split(":")[2].split("/")[0]) not in (0, 8000)
    r = httpx.post(f"{url}/chat/completions", json=_chat(), headers=_headers("one", "0.01"))
    assert r.status_code == 200


def test_start_twice_is_safe_and_stop_is_predictable(
    tmp_path: Path, upstream: _FakeUpstream
) -> None:
    config = _config(tmp_path, upstream)
    url = inferrail.start(config)
    assert inferrail.start(config) == url  # same arguments: same gateway
    with pytest.raises(RuntimeError, match="already running"):
        inferrail.start(config, model="gpt-4o")  # different arguments while running
    inferrail.stop()
    with pytest.raises(httpx.ConnectError):
        httpx.post(f"{url}/chat/completions", json=_chat(), timeout=2)
    inferrail.stop()  # nothing running: no-op
    url2 = inferrail.start(config)  # can start again after stop
    assert httpx.post(f"{url2}/chat/completions", json=_chat()).status_code == 200


def test_a_port_already_in_use_raises_and_leaves_nothing_running(
    tmp_path: Path, upstream: _FakeUpstream
) -> None:
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        with pytest.raises(RuntimeError, match="failed to start"):
            inferrail.start(_config(tmp_path, upstream), port=taken.getsockname()[1])
        assert embedded._running is None
    finally:
        taken.close()


async def test_concurrent_runs_stay_isolated_and_refusals_happen_before_the_provider(
    tmp_path: Path, upstream: _FakeUpstream
) -> None:
    url = inferrail.start(_config(tmp_path, upstream))
    async with httpx.AsyncClient(base_url=url, timeout=10) as client:

        async def call(run_id: str, budget: str) -> httpx.Response:
            return await client.post(
                "/chat/completions", json=_chat(max_tokens=200), headers=_headers(run_id, budget)
            )

        # roomy: every call fits; tight: room for two reservations (each ~$0.00012)
        results = await asyncio.gather(
            *[call("roomy", "0.01") for _ in range(8)],
            *[call("tight", "0.00025") for _ in range(8)],
        )
    roomy, tight = results[:8], results[8:]
    assert [r.status_code for r in roomy] == [200] * 8
    answered = sum(r.status_code == 200 for r in tight)
    refused = [r for r in tight if r.status_code == 402]
    assert answered == 2 and len(refused) == 6
    assert all(r.json()["error"]["code"] == "INFERRAIL_E010" for r in refused)
    assert upstream.calls == 8 + answered  # refused calls never reached the provider
    assert upstream.inferrail_headers_seen == 0
    store = ReceiptsStore(str(tmp_path / "receipts.db"))
    spent = sum((r.estimated_cost_usd or Decimal(0)) for r in store.query(work_id="tight"))
    assert spent == _CALL_COST * answered <= Decimal("0.00025")


def test_a_streamed_call_is_priced_from_its_usage(tmp_path: Path, upstream: _FakeUpstream) -> None:
    url = inferrail.start(_config(tmp_path, upstream))
    with httpx.stream(
        "POST",
        f"{url}/chat/completions",
        json=_chat(stream=True),
        headers=_headers("streamed", "0.01"),
    ) as r:
        assert r.status_code == 200
        lines = [line for line in r.iter_lines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    receipts = ReceiptsStore(str(tmp_path / "receipts.db")).query(work_id="streamed")
    assert len(receipts) == 1 and receipts[0].estimated_cost_usd == _CALL_COST
    assert upstream.inferrail_headers_seen == 0


def test_retrying_a_refused_call_never_reaches_the_provider(
    tmp_path: Path, upstream: _FakeUpstream
) -> None:
    url = inferrail.start(_config(tmp_path, upstream))
    tiny = _headers("too-small", "0.00001")
    statuses = [
        httpx.post(f"{url}/chat/completions", json=_chat(), headers=tiny).status_code
        for _ in range(3)
    ]
    assert statuses == [402, 402, 402]
    assert upstream.calls == 0


class _OkProvider:
    name = "openai"

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        return NormalizedChatResponse(
            content="ok", finish_reason="stop", prompt_tokens=100, completion_tokens=50
        )


def test_zero_config_start_stores_runs_where_inferrail_work_finds_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    monkeypatch.setattr(embedded, "ensure_app_data_dir", lambda: app_data)
    monkeypatch.setattr(cli_main, "app_data_dir", lambda: app_data)
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": _OkProvider()})
    monkeypatch.chdir(tmp_path)  # no inferrail.yaml, no quickstart file here

    url = inferrail.start()
    run = _headers("run-7f3a", "0.00015")  # room for one ~$0.00012 reservation
    ok = httpx.post(f"{url}/chat/completions", json=_chat(max_tokens=200), headers=run)
    refused = httpx.post(f"{url}/chat/completions", json=_chat(max_tokens=200), headers=run)
    assert (ok.status_code, refused.status_code) == (200, 402)
    assert (app_data / "receipts.db").exists() and (app_data / "budgets.db").exists()
    assert not list(tmp_path.glob("*.yaml"))

    assert cli_main.main(["work", "run-7f3a"]) == 0
    out = capsys.readouterr().out
    assert "Work:                              run-7f3a" in out
    assert "Inference receipts:                 2" in out


def test_work_lookup_prefers_the_file_that_has_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An older quickstart JSONL in the cwd doesn't hide a run that only
    the app-data receipts have; a run in the cwd file still comes from it."""
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    monkeypatch.setattr(cli_main, "app_data_dir", lambda: app_data)
    monkeypatch.chdir(tmp_path)
    from inferrail.receipts.schema import InferenceReceipt

    def receipt(work_id: str) -> InferenceReceipt:
        return InferenceReceipt(
            receipt_id=f"ir_{work_id}",
            request_id="r",
            route="default",
            provider="openai",
            model="gpt-4o-mini",
            status="success",
            total_latency_ms=1.0,
            attributes={"work_id": work_id},
        )

    (tmp_path / "inferrail-receipts.jsonl").write_text(receipt("old-run").model_dump_json() + "\n")
    ReceiptsStore(str(app_data / "receipts.db")).emit(receipt("new-run"))

    assert cli_main.main(["work", "new-run"]) == 0
    assert "Work:                              new-run" in capsys.readouterr().out
    assert cli_main.main(["work", "old-run"]) == 0
    assert "Work:                              old-run" in capsys.readouterr().out


class _AnyModelProvider:
    """Answers any model id; records which model it was asked for."""

    name = "openai"

    def __init__(self) -> None:
        self.models: list[str] = []

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        self.models.append(request.model)
        return NormalizedChatResponse(
            content="ok", finish_reason="stop", prompt_tokens=100, completion_tokens=50
        )


@pytest.fixture
def zero_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _AnyModelProvider:
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    provider = _AnyModelProvider()
    monkeypatch.setattr(embedded, "ensure_app_data_dir", lambda: app_data)
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    return provider


def test_zero_config_start_picks_no_model_and_refuses_unpriced_models_under_a_budget(
    zero_config: _AnyModelProvider,
) -> None:
    url = inferrail.start()
    run = _headers("new-model-run", "0.50")
    unpriced = httpx.post(
        f"{url}/chat/completions", json=_chat(model="brand-new-model"), headers=run
    )
    assert unpriced.status_code == 402
    assert unpriced.json()["error"]["code"] == "INFERRAIL_E012"
    # Without a budget the same model is simply passed through (cost unknown).
    assert (
        httpx.post(f"{url}/chat/completions", json=_chat(model="brand-new-model")).status_code
        == 200
    )
    # No hidden alias: "default" is sent to the provider as-is, not swapped for a model.
    httpx.post(f"{url}/chat/completions", json=_chat(model="default"))
    assert zero_config.models == ["brand-new-model", "default"]


def test_start_with_operator_pricing_budgets_a_new_model(zero_config: _AnyModelProvider) -> None:
    url = inferrail.start(
        pricing={
            "openai": {
                "brand-new-model": {
                    "input_usd_per_million": "1.00",
                    "output_usd_per_million": "4.00",
                    "source": "vendor pricing page",
                    "verified_date": "2026-10-01",
                }
            }
        }
    )
    r = httpx.post(
        f"{url}/chat/completions",
        json=_chat(model="brand-new-model", max_tokens=200),
        headers=_headers("priced-run", "0.50"),
    )
    assert r.status_code == 200


def test_start_model_only_names_the_default_alias(zero_config: _AnyModelProvider) -> None:
    url = inferrail.start(model="chosen-model")
    httpx.post(f"{url}/chat/completions", json=_chat(model="default"))
    httpx.post(f"{url}/chat/completions", json=_chat(model="other-model"))
    assert zero_config.models == ["chosen-model", "other-model"]


def test_model_and_pricing_are_zero_config_only(tmp_path: Path, upstream: _FakeUpstream) -> None:
    with pytest.raises(ValueError, match="zero-config"):
        inferrail.start(_config(tmp_path, upstream), model="x")
