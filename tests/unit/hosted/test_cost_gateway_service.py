"""Service-wiring tests for hosted/cost_gateway/service.py: trial
issuance, per-tenant isolation, key handling (never echoed/leaked,
cross-tenant isolation, expiry tightening), demo mode, budget
enforcement, rate limiting, work/report/transaction rollups, and
CLI-behavior parity (Phase 3).

Uses only dependencies already required by `inferrail` core (fastapi,
pydantic, httpx) -- no optional extra needed. Real-provider proxying
(`/v1/chat/completions`, `/v1/messages`) IS exercised here, against
`httpx.MockTransport` rather than real network access -- the same
pattern `tests/unit/test_providers.py` already uses for the exact same
`OpenAIProvider`/`AnthropicProvider` classes this service constructs.
`service.py`'s `openai_client_factory`/`anthropic_client_factory`
module-level functions exist specifically as the monkeypatch seam that
makes this possible without a real key or a live network call. A real,
billed live-provider call remains a manual step (see
`hosted/cost_gateway/smoke_test_live_key.sh`), same "flag, don't hide"
pattern the AP release used for its own `OPENAI_API_KEY`-gated tests --
but everything about request/response *shape*, streaming, tool-call
passthrough, and attribution is now verified here, not left unverified.

Loads hosted/cost_gateway's flat-file modules by explicit path via
`importlib.util`, registering them under their own plain names ("auth",
"tenant_store", "trial", "keys", "demo_provider") -- the same pattern
`test_ap_exceptions_service.py` already established, and the same
caveat applies: `hosted/ap_exceptions` has its own same-named
"auth"/"tenant_store" modules. Because both test files' fixtures are
function-scoped and reload their own versions into `sys.modules`
immediately before constructing their own app, within any single test
function the names always resolve to *this* directory's modules
regardless of what any other test file did earlier in the same pytest
session -- see that module's docstring for the fuller explanation, which
applies here unchanged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.schemas import ChatCompletionRequest
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.base import ChatMessage
from inferrail.providers.openai import OpenAIProvider
from inferrail.receipts.sinks import JSONLReceiptSink
from inferrail.routing.router import Router
from inferrail.telemetry.sinks import NullTelemetrySink

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "cost_gateway"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOSTED_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service_module(monkeypatch, tmp_path):
    monkeypatch.setenv("COST_GATEWAY_RATE_LIMIT_MAX_REQUESTS", "1000")
    monkeypatch.setenv("COST_GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("COST_GATEWAY_ISSUE_MAX_PER_IP", "1000")
    monkeypatch.setenv("COST_GATEWAY_DEMO_TTL_SECONDS", "3600")
    monkeypatch.setenv("COST_GATEWAY_REAL_KEY_TTL_SECONDS", "60")
    monkeypatch.setenv("COST_GATEWAY_DAILY_BUDGET_USD", "1.00")
    _load("trial", "trial.py")
    _load("keys", "keys.py")
    _load("tenant_store", "tenant_store.py")
    _load("auth", "auth.py")
    _load("demo_provider", "demo_provider.py")
    return _load("cost_gateway_service_under_test", "service.py")


@pytest.fixture
def app(service_module, tmp_path):
    return service_module.create_app(tmp_path / "data")


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def _issue(client) -> dict:
    resp = client.post("/v1/trial")
    assert resp.status_code == 200
    return resp.json()


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _demo_payload() -> dict:
    return {"model": "anything", "messages": [{"role": "user", "content": "hello"}]}


# --- health / issuance -------------------------------------------------


def test_health_requires_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_trial_returns_api_key_and_defaults_to_demo_mode(client):
    trial = _issue(client)
    assert trial["api_key"].startswith("trial_")
    assert trial["mode"] == "demo"
    assert trial["openai_configured"] is False
    assert trial["anthropic_configured"] is False
    assert trial["seconds_remaining"] > 0
    assert trial["dashboard_url"] is None


# --- auth ----------------------------------------------------------------


def test_missing_auth_header_is_401(client):
    resp = client.get("/v1/receipts")
    assert resp.status_code == 401


def test_invalid_api_key_is_401(client):
    resp = client.get("/v1/receipts", headers=_auth("not-a-real-key"))
    assert resp.status_code == 401


def test_expired_trial_is_401(service_module, tmp_path, monkeypatch):
    monkeypatch.setenv("COST_GATEWAY_DEMO_TTL_SECONDS", "0.05")
    app = service_module.create_app(tmp_path / "data")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    trial = _issue(client)
    time.sleep(0.2)
    resp = client.get(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"]


def test_tenant_id_mismatch_in_path_is_403(client):
    trial_a = _issue(client)
    trial_b = _issue(client)
    resp = client.get(
        f"/v1/trial/{trial_b['tenant_id']}", headers=_auth(trial_a["api_key"])
    )
    assert resp.status_code == 403


# --- demo mode -------------------------------------------------------------


def test_demo_chat_completion_works_without_any_key(client):
    trial = _issue(client)
    resp = client.post(
        "/v1/demo/chat/completions", json=_demo_payload(), headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["prompt_tokens"] is not None


def test_demo_receipt_appears_in_receipts_listing(client):
    trial = _issue(client)
    client.post(
        "/v1/demo/chat/completions",
        json=_demo_payload(),
        headers={**_auth(trial["api_key"]), "X-Inferrail-Attribute-Customer": "acme"},
    )
    resp = client.get("/v1/receipts", headers=_auth(trial["api_key"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    receipt = body["receipts"][0]
    assert receipt["provider"] == "demo"
    assert receipt["status"] == "success"
    assert receipt["estimated_cost_usd"] is not None
    assert receipt["attributes"] == {"customer": "acme"}
    # Structural payload-free guarantee: no field anywhere resembling
    # prompt/response content.
    assert "content" not in receipt
    assert "message" not in receipt
    assert "prompt" not in receipt


# --- real-key gating (no network exercised) ---------------------------------


def test_real_chat_completions_requires_key_returns_400(client):
    trial = _issue(client)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 400
    assert "no OpenAI key configured" in resp.json()["detail"]


def test_real_messages_requires_key_returns_400(client):
    trial = _issue(client)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 400
    assert "no Anthropic key configured" in resp.json()["detail"]


# --- key submission, validation, and leakage ------------------------------


def test_submit_keys_requires_at_least_one(client):
    trial = _issue(client)
    resp = client.post(
        f"/v1/trial/{trial['tenant_id']}/keys", json={}, headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 422


def test_submit_keys_rejects_whitespace_key_without_echoing_value(client):
    trial = _issue(client)
    secret = "sk-has a space in it"
    resp = client.post(
        f"/v1/trial/{trial['tenant_id']}/keys",
        json={"openai_key": secret},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 422
    assert secret not in resp.text


def test_submit_keys_marks_openai_configured_and_tightens_expiry(client):
    trial = _issue(client)
    before_remaining = trial["seconds_remaining"]
    resp = client.post(
        f"/v1/trial/{trial['tenant_id']}/keys",
        json={"openai_key": "sk-fake-key-for-tests"},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["openai_configured"] is True
    assert body["anthropic_configured"] is False
    assert body["mode"] == "real_key"
    # COST_GATEWAY_REAL_KEY_TTL_SECONDS=60 in the fixture, well under the
    # 3600s demo TTL -- expiry must have tightened, never extended.
    assert body["seconds_remaining"] <= 60
    assert body["seconds_remaining"] < before_remaining


def test_keys_never_appear_in_any_response_body(client):
    trial = _issue(client)
    secret = "sk-supersecretmarkervalue12345"
    submit_resp = client.post(
        f"/v1/trial/{trial['tenant_id']}/keys",
        json={"openai_key": secret},
        headers=_auth(trial["api_key"]),
    )
    assert secret not in submit_resp.text
    status_resp = client.get(
        f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"])
    )
    assert secret not in status_resp.text
    bad_call_resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(trial["api_key"]),
    )
    assert secret not in bad_call_resp.text


def test_cross_tenant_key_isolation(client):
    trial_a = _issue(client)
    trial_b = _issue(client)
    client.post(
        f"/v1/trial/{trial_a['tenant_id']}/keys",
        json={"openai_key": "sk-tenant-a-only"},
        headers=_auth(trial_a["api_key"]),
    )
    status_b = client.get(
        f"/v1/trial/{trial_b['tenant_id']}", headers=_auth(trial_b["api_key"])
    ).json()
    assert status_b["openai_configured"] is False
    assert status_b["mode"] == "demo"
    # Tenant B cannot even ask about tenant A's status.
    cross = client.get(
        f"/v1/trial/{trial_a['tenant_id']}", headers=_auth(trial_b["api_key"])
    )
    assert cross.status_code == 403


def test_cross_tenant_receipts_isolation(client):
    trial_a = _issue(client)
    trial_b = _issue(client)
    client.post(
        "/v1/demo/chat/completions", json=_demo_payload(), headers=_auth(trial_a["api_key"])
    )
    receipts_b = client.get("/v1/receipts", headers=_auth(trial_b["api_key"])).json()
    assert receipts_b["total"] == 0
    receipts_a = client.get("/v1/receipts", headers=_auth(trial_a["api_key"])).json()
    assert receipts_a["total"] == 1


# --- deletion / teardown ----------------------------------------------------


def test_delete_keys_forgets_key(client):
    trial = _issue(client)
    client.post(
        f"/v1/trial/{trial['tenant_id']}/keys",
        json={"openai_key": "sk-to-be-forgotten"},
        headers=_auth(trial["api_key"]),
    )
    del_resp = client.delete(
        f"/v1/trial/{trial['tenant_id']}/keys", headers=_auth(trial["api_key"])
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["keys_removed"] is True
    status = client.get(
        f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"])
    ).json()
    assert status["openai_configured"] is False


def test_end_trial_removes_tenant(client):
    trial = _issue(client)
    resp = client.delete(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    assert resp.status_code == 200
    assert resp.json()["ended"] is True
    # The API key is now dead -- any further authenticated call is 401.
    follow_up = client.get(
        f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"])
    )
    assert follow_up.status_code == 401


# --- rate limiting / budget --------------------------------------------------


def test_rate_limit_enforced(service_module, tmp_path, monkeypatch):
    monkeypatch.setenv("COST_GATEWAY_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("COST_GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "60")
    app = service_module.create_app(tmp_path / "data")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    trial = _issue(client)
    # Issuance itself doesn't count against the per-tenant limiter (it's
    # unauthenticated) -- two authenticated calls should be the first two
    # to actually consume the budget.
    r1 = client.get(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    r2 = client.get(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    r3 = client.get(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429


def test_daily_budget_blocks_demo_requests_when_exceeded(
    service_module, tmp_path, monkeypatch
):
    monkeypatch.setenv("COST_GATEWAY_DAILY_BUDGET_USD", "0.0000001")
    app = service_module.create_app(tmp_path / "data")
    from fastapi.testclient import TestClient

    client = TestClient(app)
    trial = _issue(client)
    resp = client.post(
        "/v1/demo/chat/completions", json=_demo_payload(), headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 402
    assert resp.json()["error"]["code"]


# --- CORS (required for the Phase 2 browser-based frontend) ----------------


def test_cors_preflight_allows_browser_frontend(client):
    resp = client.options(
        "/v1/trial",
        headers={
            "Origin": "https://tryinferrail.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


def test_cors_headers_present_on_actual_response(client):
    resp = client.get("/health", headers={"Origin": "https://tryinferrail.com"})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


# --- Work economics, transaction, and report rollups (Phase 3) -------------


def test_work_outcome_and_summary_roundtrip(client):
    trial = _issue(client)
    client.post(
        "/v1/demo/chat/completions",
        json=_demo_payload(),
        headers={**_auth(trial["api_key"]), "X-Inferrail-Attribute-Work-Id": "wid-1"},
    )
    outcome_resp = client.post(
        "/v1/work/wid-1/outcome",
        json={"outcome_status": "resolved"},
        headers=_auth(trial["api_key"]),
    )
    assert outcome_resp.status_code == 200
    summary = client.get("/v1/work/wid-1", headers=_auth(trial["api_key"])).json()
    assert summary["work_id"] == "wid-1"
    assert summary["outcome_status"] == "resolved"
    assert summary["receipt_count"] == 1
    assert summary["known_attributed_inference_cost_usd"] == "0.000160"


def test_work_summary_404_for_unknown_work_id(client):
    trial = _issue(client)
    resp = client.get("/v1/work/does-not-exist", headers=_auth(trial["api_key"]))
    assert resp.status_code == 404


def test_list_work_summaries_returns_every_known_work_id(client):
    trial = _issue(client)
    for wid in ("wid-a", "wid-b"):
        client.post(
            "/v1/demo/chat/completions",
            json=_demo_payload(),
            headers={**_auth(trial["api_key"]), "X-Inferrail-Attribute-Work-Id": wid},
        )
    body = client.get("/v1/work", headers=_auth(trial["api_key"])).json()
    assert {w["work_id"] for w in body["work"]} == {"wid-a", "wid-b"}


def test_transaction_groups_by_task_id(client):
    trial = _issue(client)
    for _ in range(2):
        client.post(
            "/v1/demo/chat/completions",
            json=_demo_payload(),
            headers={**_auth(trial["api_key"]), "X-Inferrail-Attribute-Task-Id": "task-1"},
        )
    tx = client.get("/v1/transaction/task-1", headers=_auth(trial["api_key"])).json()
    assert tx["task_id"] == "task-1"
    assert len(tx["events"]) == 2
    assert tx["known_total_cost_usd"] == "0.000320"
    assert tx["status"] == "success"


def test_transaction_404_when_no_matching_receipts(client):
    trial = _issue(client)
    resp = client.get("/v1/transaction/nope", headers=_auth(trial["api_key"]))
    assert resp.status_code == 404


def test_report_groups_by_arbitrary_attribute_and_labels_unattributed(client):
    trial = _issue(client)
    for _ in range(2):
        client.post(
            "/v1/demo/chat/completions",
            json=_demo_payload(),
            headers={**_auth(trial["api_key"]), "X-Inferrail-Attribute-Customer": "acme"},
        )
    client.post("/v1/demo/chat/completions", json=_demo_payload(), headers=_auth(trial["api_key"]))
    report = client.get("/v1/report?by=customer", headers=_auth(trial["api_key"])).json()
    rows = {r["group"]: r for r in report["rows"]}
    assert rows["acme"]["receipt_count"] == 2
    assert rows["acme"]["known_cost_usd"] == "0.000320"
    assert rows["(unattributed)"]["receipt_count"] == 1


def test_work_and_report_are_isolated_per_tenant(client):
    trial_a = _issue(client)
    trial_b = _issue(client)
    client.post(
        "/v1/demo/chat/completions",
        json=_demo_payload(),
        headers={**_auth(trial_a["api_key"]), "X-Inferrail-Attribute-Work-Id": "shared-id"},
    )
    assert client.get("/v1/work/shared-id", headers=_auth(trial_a["api_key"])).status_code == 200
    assert client.get("/v1/work/shared-id", headers=_auth(trial_b["api_key"])).status_code == 404


# --- Real-provider parity via httpx.MockTransport (Phase 3) ----------------
#
# Mirrors tests/unit/test_providers.py's own MockTransport pattern against
# the exact same OpenAIProvider class -- proves request/response shape,
# streaming, tool-call passthrough, and attribution all match the
# self-hosted gateway's behavior, without a real network call or key.


def _mock_openai_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("tools"):
        payload = {
            "id": "chatcmpl-mock",
            "model": body["model"],
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":   "SF",  "unit": "celsius"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8},
        }
    else:
        payload = {
            "id": "chatcmpl-mock",
            "model": body["model"],
            "choices": [
                {"message": {"role": "assistant", "content": "hi there"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        }
    return httpx.Response(200, json=payload)


def _mock_openai_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_mock_openai_handler))


def _mock_openai_stream_client() -> httpx.AsyncClient:
    raw_body = (
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":9,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, content=raw_body, headers={"content-type": "text/event-stream"}
        )
    )
    return httpx.AsyncClient(transport=transport)


def _with_openai_key(client, trial):
    resp = client.post(
        f"/v1/trial/{trial['tenant_id']}/keys",
        json={"openai_key": "sk-fake-for-mock-tests"},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 200


def test_real_chat_completions_matches_openai_shaped_response(service_module, client, monkeypatch):
    monkeypatch.setattr(service_module, "openai_client_factory", _mock_openai_client)
    trial = _issue(client)
    _with_openai_key(client, trial)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={**_auth(trial["api_key"]), "X-Inferrail-Attribute-Customer": "acme"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "hi there"
    assert body["usage"]["prompt_tokens"] == 12
    assert body["inferrail"]["provider"] == "openai"
    receipts = client.get("/v1/receipts", headers=_auth(trial["api_key"])).json()["receipts"]
    assert receipts[0]["provider"] == "openai"
    assert receipts[0]["model"] == "gpt-4o-mini"
    assert receipts[0]["attributes"] == {"customer": "acme"}
    assert receipts[0]["estimated_cost_usd"] is not None  # real builtin-catalog price, not DEMO


def test_real_chat_completions_tool_call_passthrough_byte_exact(
    service_module, client, monkeypatch
):
    monkeypatch.setattr(service_module, "openai_client_factory", _mock_openai_client)
    trial = _issue(client)
    _with_openai_key(client, trial)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
        },
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 200
    tool_call = resp.json()["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "get_weather"
    # Byte-exact, unusual spacing preserved -- never reparsed/reformatted,
    # same guarantee providers.base's module docstring makes.
    assert tool_call["function"]["arguments"] == '{"city":   "SF",  "unit": "celsius"}'


def test_real_chat_completions_streaming_forwards_raw_sse(service_module, client, monkeypatch):
    monkeypatch.setattr(service_module, "openai_client_factory", _mock_openai_stream_client)
    trial = _issue(client)
    _with_openai_key(client, trial)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers=_auth(trial["api_key"]),
    ) as resp:
        assert resp.status_code == 200
        chunks = b"".join(resp.iter_bytes())
    assert b'"content":"Hel"' in chunks
    assert b'"content":"lo"' in chunks
    assert b"[DONE]" in chunks
    # The stream is still accounted for: a receipt exists afterward with
    # real usage recovered from the SSE bookkeeper, same as self-hosted.
    receipts = client.get("/v1/receipts", headers=_auth(trial["api_key"])).json()["receipts"]
    assert receipts[0]["prompt_tokens"] == 9
    assert receipts[0]["completion_tokens"] == 2


def test_hosted_and_local_gateway_produce_structurally_equivalent_results(
    service_module, client, monkeypatch, tmp_path
):
    """The explicit Phase 3 parity proof: the exact same request, sent
    through (a) a local InferenceEngine built the same way `inferrail
    serve --quickstart` builds one, and (b) this hosted service's
    `/v1/chat/completions`, against the identical mocked upstream --
    produces the same response content/usage and the same receipt shape.
    A real client only ever has to change its `base_url`, never its
    request-building code, to move between the two.
    """
    request = ChatCompletionRequest(
        model="gpt-4o-mini", messages=[ChatMessage(role="user", content="hi")]
    )
    attributes = {"customer": "acme"}

    # (a) Local, self-hosted-shaped engine -- same classes `inferrail
    # serve` itself builds (gateway.execution.InferenceEngine,
    # providers.openai.OpenAIProvider, routing.router.Router).
    local_provider = OpenAIProvider(
        name="openai",
        api_key="sk-local",
        base_url="https://example.invalid/v1",
        is_verified_openai=True,
        client=_mock_openai_client(),
    )
    local_engine = InferenceEngine(
        router=Router(routes={}, default_provider="openai"),
        providers={"openai": local_provider},
        telemetry=NullTelemetrySink(),
        pricing_resolver=PricingResolver(providers={}, overrides={}),
        receipts=JSONLReceiptSink(tmp_path / "local-receipts.jsonl"),
    )
    local_result = asyncio.run(local_engine.execute(request, attributes=dict(attributes)))

    # (b) The hosted trial -- identical request, identical mocked upstream.
    monkeypatch.setattr(service_module, "openai_client_factory", _mock_openai_client)
    trial = _issue(client)
    _with_openai_key(client, trial)
    hosted_resp = client.post(
        "/v1/chat/completions",
        json=request.model_dump(mode="json"),
        headers={**_auth(trial["api_key"]), "X-Inferrail-Attribute-Customer": "acme"},
    )
    hosted_body = hosted_resp.json()

    assert hosted_resp.status_code == 200
    local_content = local_result.choices[0].message.content
    assert hosted_body["choices"][0]["message"]["content"] == local_content
    assert hosted_body["usage"]["prompt_tokens"] == local_result.usage.prompt_tokens
    assert hosted_body["usage"]["completion_tokens"] == local_result.usage.completion_tokens
    assert hosted_body["inferrail"]["provider"] == local_result.inferrail.provider

    hosted_receipt = client.get(
        "/v1/receipts", headers=_auth(trial["api_key"])
    ).json()["receipts"][0]
    assert hosted_receipt["provider"] == "openai"
    assert hosted_receipt["model"] == "gpt-4o-mini"
    assert hosted_receipt["prompt_tokens"] == local_result.usage.prompt_tokens
    assert hosted_receipt["completion_tokens"] == local_result.usage.completion_tokens
    assert hosted_receipt["attributes"] == attributes


# --- Feedback + admin usage stats -------------------------------------------


def test_submit_feedback_requires_auth(client):
    resp = client.post("/v1/feedback", json={"message": "hi"})
    assert resp.status_code == 401


def test_submit_feedback_rejects_empty_message(client):
    trial = _issue(client)
    resp = client.post(
        "/v1/feedback", json={"message": "   "}, headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 422


def test_admin_routes_404_when_no_admin_token_configured(client):
    # No COST_GATEWAY_ADMIN_TOKEN set in this fixture's environment.
    assert client.get("/v1/admin/stats").status_code == 404
    assert client.get("/v1/admin/feedback").status_code == 404


def test_admin_stats_and_feedback_require_correct_token(service_module, tmp_path, monkeypatch):
    monkeypatch.setenv("COST_GATEWAY_ADMIN_TOKEN", "super-secret-admin-token")
    app = service_module.create_app(tmp_path / "data")
    from fastapi.testclient import TestClient

    admin_client = TestClient(app)
    trial = _issue(admin_client)
    admin_client.post(
        "/v1/feedback",
        json={"message": "the demo button felt slow", "contact": "user@example.com"},
        headers=_auth(trial["api_key"]),
    )

    # Wrong/missing token -- rejected, not just silently empty.
    assert admin_client.get("/v1/admin/stats").status_code == 401
    assert (
        admin_client.get(
            "/v1/admin/stats", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )

    stats = admin_client.get(
        "/v1/admin/stats", headers={"Authorization": "Bearer super-secret-admin-token"}
    ).json()
    assert stats["trials_issued_total"] == 1
    assert stats["trials_live_now"] == 1
    assert stats["feedback_count"] == 1

    feedback = admin_client.get(
        "/v1/admin/feedback", headers={"Authorization": "Bearer super-secret-admin-token"}
    ).json()
    assert feedback["total"] == 1
    row = feedback["feedback"][0]
    assert row["message"] == "the demo button felt slow"
    assert row["contact"] == "user@example.com"
    assert row["tenant_id"] == trial["tenant_id"]


def test_feedback_survives_trial_ending(service_module, tmp_path, monkeypatch):
    monkeypatch.setenv("COST_GATEWAY_ADMIN_TOKEN", "another-secret")
    app = service_module.create_app(tmp_path / "data")
    from fastapi.testclient import TestClient

    admin_client = TestClient(app)
    trial = _issue(admin_client)
    admin_client.post(
        "/v1/feedback", json={"message": "found a bug"}, headers=_auth(trial["api_key"])
    )
    admin_client.delete(f"/v1/trial/{trial['tenant_id']}", headers=_auth(trial["api_key"]))
    feedback = admin_client.get(
        "/v1/admin/feedback", headers={"Authorization": "Bearer another-secret"}
    ).json()
    assert feedback["total"] == 1
    assert feedback["feedback"][0]["message"] == "found a bug"


# --- Feedback -> GitHub Issues (best-effort, never blocks the submission) --


def test_feedback_without_github_token_configured_skips_silently(client):
    trial = _issue(client)
    resp = client.post(
        "/v1/feedback", json={"message": "no github token set"}, headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 200
    assert resp.json()["received"] is True
    assert resp.json()["github_issue_url"] is None


FEEDBACK_REPO = "example-owner/private-feedback"


def _mock_github(monkeypatch, *, private: bool = True, issue_status: int = 201) -> list:
    """Routes every httpx.AsyncClient the feedback sink builds through a
    MockTransport standing in for the GitHub API; returns the list of
    captured requests. `_GitHubFeedbackSink` constructs its own client
    internally rather than accepting an injectable one (unlike the
    provider client-factory seam), since it has no per-tenant identity to
    key an injection point on."""
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"full_name": FEEDBACK_REPO, "private": private})
        return httpx.Response(
            issue_status, json={"html_url": f"https://github.com/{FEEDBACK_REPO}/issues/999"}
        )

    real_async_client = httpx.AsyncClient

    def mock_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)
    return captured


def _feedback_client(service_module, tmp_path):
    from fastapi.testclient import TestClient

    return TestClient(service_module.create_app(tmp_path / "data"))


def test_feedback_creates_github_issue_when_configured(service_module, monkeypatch, tmp_path):
    monkeypatch.setenv("COST_GATEWAY_GITHUB_TOKEN", "fake-github-token")
    monkeypatch.setenv("COST_GATEWAY_GITHUB_REPO", FEEDBACK_REPO)
    captured = _mock_github(monkeypatch)

    gh_client = _feedback_client(service_module, tmp_path)
    trial = _issue(gh_client)
    resp = gh_client.post(
        "/v1/feedback",
        json={"message": "found a real bug\nwith details", "contact": "me@example.com"},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 200
    assert resp.json()["github_issue_url"] == f"https://github.com/{FEEDBACK_REPO}/issues/999"

    # Privacy check first, then the issue itself.
    check, create = captured
    assert check.method == "GET"
    assert str(check.url) == f"https://api.github.com/repos/{FEEDBACK_REPO}"
    assert create.method == "POST"
    assert str(create.url) == f"https://api.github.com/repos/{FEEDBACK_REPO}/issues"
    assert create.headers.get("authorization") == "Bearer fake-github-token"
    body = json.loads(create.content)
    assert body["title"] == "[Cost Gateway feedback] found a real bug"
    assert "with details" in body["body"]
    assert "me@example.com" in body["body"]
    assert body["labels"] == ["cost-gateway-feedback"]

    # The local write still happened too -- GitHub is additive, not a
    # replacement for the always-on local record.
    monkeypatch.setenv("COST_GATEWAY_ADMIN_TOKEN", "admin-secret-for-this-test")
    # Recreate the app so the admin route picks up the freshly-set env var
    # (admin_token is read once at create_app() time).
    admin_client = _feedback_client(service_module, tmp_path)
    feedback = admin_client.get(
        "/v1/admin/feedback", headers={"Authorization": "Bearer admin-secret-for-this-test"}
    ).json()
    assert feedback["total"] == 1


def test_feedback_never_filed_to_a_public_repo(service_module, monkeypatch, tmp_path):
    """Feedback can contain a visitor's email -- it must never land on a
    public issue tracker, even if the operator misconfigures the repo."""
    monkeypatch.setenv("COST_GATEWAY_GITHUB_TOKEN", "fake-github-token")
    monkeypatch.setenv("COST_GATEWAY_GITHUB_REPO", FEEDBACK_REPO)
    captured = _mock_github(monkeypatch, private=False)

    gh_client = _feedback_client(service_module, tmp_path)
    trial = _issue(gh_client)
    resp = gh_client.post(
        "/v1/feedback",
        json={"message": "private details", "contact": "me@example.com"},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 200
    assert resp.json()["received"] is True
    assert resp.json()["github_issue_url"] is None
    assert [r.method for r in captured] == ["GET"]


def test_feedback_not_filed_without_an_explicit_repo(service_module, monkeypatch, tmp_path):
    """No default repo: a token alone never causes an issue to be filed
    anywhere."""
    monkeypatch.setenv("COST_GATEWAY_GITHUB_TOKEN", "fake-github-token")
    monkeypatch.delenv("COST_GATEWAY_GITHUB_REPO", raising=False)
    captured = _mock_github(monkeypatch)

    gh_client = _feedback_client(service_module, tmp_path)
    trial = _issue(gh_client)
    resp = gh_client.post(
        "/v1/feedback", json={"message": "hello"}, headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 200
    assert resp.json()["github_issue_url"] is None
    assert captured == []


def test_feedback_per_trial_cap(service_module, monkeypatch, tmp_path):
    monkeypatch.setattr(service_module, "FEEDBACK_MAX_PER_TENANT", 2)
    fb_client = _feedback_client(service_module, tmp_path)
    trial = _issue(fb_client)
    for i in range(2):
        resp = fb_client.post(
            "/v1/feedback", json={"message": f"report {i}"}, headers=_auth(trial["api_key"])
        )
        assert resp.status_code == 200
    resp = fb_client.post(
        "/v1/feedback", json={"message": "one too many"}, headers=_auth(trial["api_key"])
    )
    assert resp.status_code == 429

    # The cap is per trial, not global.
    other = _issue(fb_client)
    resp = fb_client.post(
        "/v1/feedback", json={"message": "different trial"}, headers=_auth(other["api_key"])
    )
    assert resp.status_code == 200


def test_feedback_github_issues_capped_per_hour(service_module, monkeypatch, tmp_path):
    """Past the global hourly cap, feedback is still accepted and saved
    locally -- only the GitHub copy is skipped."""
    monkeypatch.setenv("COST_GATEWAY_GITHUB_TOKEN", "fake-github-token")
    monkeypatch.setenv("COST_GATEWAY_GITHUB_REPO", FEEDBACK_REPO)
    monkeypatch.setattr(service_module, "FEEDBACK_ISSUES_MAX_PER_HOUR", 1)
    captured = _mock_github(monkeypatch)

    gh_client = _feedback_client(service_module, tmp_path)
    first = _issue(gh_client)
    second = _issue(gh_client)
    resp1 = gh_client.post(
        "/v1/feedback", json={"message": "first"}, headers=_auth(first["api_key"])
    )
    resp2 = gh_client.post(
        "/v1/feedback", json={"message": "second"}, headers=_auth(second["api_key"])
    )
    assert resp1.json()["github_issue_url"] is not None
    assert resp2.status_code == 200
    assert resp2.json()["received"] is True
    assert resp2.json()["github_issue_url"] is None
    assert [r.method for r in captured] == ["GET", "POST"]


def test_feedback_submission_still_succeeds_if_github_api_fails(
    service_module, monkeypatch, tmp_path
):
    monkeypatch.setenv("COST_GATEWAY_GITHUB_TOKEN", "fake-github-token")
    monkeypatch.setenv("COST_GATEWAY_GITHUB_REPO", FEEDBACK_REPO)

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    real_async_client = httpx.AsyncClient

    def mock_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(failing_handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)

    app = service_module.create_app(tmp_path / "data")
    from fastapi.testclient import TestClient

    gh_client = TestClient(app)
    trial = _issue(gh_client)
    resp = gh_client.post(
        "/v1/feedback", json={"message": "still saved locally even if github fails"},
        headers=_auth(trial["api_key"]),
    )
    assert resp.status_code == 200
    assert resp.json()["received"] is True
    assert resp.json()["github_issue_url"] is None
