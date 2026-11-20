"""Web Read's paid MCP tool, driven over real MCP streamable HTTP (JSON-RPC).

Proves: the tool is listed with its input schema; an unpaid call returns the x402
PaymentRequired (same network, payTo and price as the HTTP route); a call paid with the x402
MCP transport (`_meta["x402/payment"]`, signed by the SDK's buyer client) delivers the page,
settles once and is recorded in the ledger; the same payment presented again is never fetched
or settled twice; a refused page cancels the payment; and nothing is mounted by default.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("x402")
pytest.importorskip("eth_account")
pytest.importorskip("mcp")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import FakeFacilitator  # noqa: E402
from eth_account import Account  # noqa: E402
from mcp.types import LATEST_PROTOCOL_VERSION  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount  # noqa: E402
from x402.client import x402ClientSync  # noqa: E402
from x402.mcp import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY  # noqa: E402
from x402.mechanisms.evm.exact import ExactEvmScheme  # noqa: E402
from x402.schemas import PaymentRequired  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


svc = sys.modules.get("web_read_service") or _load(
    "web_read_service", ROOT / "hosted" / "web_read" / "service.py"
)
fetch = sys.modules["web_read_fetch"]
PAY_TO = "0x" + "7" * 40
ARTICLE = (
    "<html><head><title>T</title></head><body>"
    "<main><h1>Hello</h1><p>World.</p></main></body></html>"
)
SITE = {
    "news.test/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
    "news.test/a": httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"}),
}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _site() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        r = SITE.get(f"{request.url.host}{request.url.path}")
        return (
            httpx.Response(404)
            if r is None
            else httpx.Response(r.status_code, headers=r.headers, content=r.content)
        )

    return httpx.MockTransport(handler)


async def _dns(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


@contextlib.contextmanager
def _server(tmp_path: Path, fac: FakeFacilitator, mcp: bool = True):
    env = {
        "READ_PAY_TO": PAY_TO, "READ_RESOURCE_URL": "http://testserver/v1/page",
        "READ_DATA_DIR": str(tmp_path), "BG_OPERATOR_TOKEN": "op-token",
    }  # fmt: skip
    if mcp:
        env["READ_MCP"] = "1"
    settings = svc.Settings.from_env(env)
    reader = fetch.Fetcher(settings.user_agent, transport=_site(), resolve=_dns)
    web = svc.create_app(settings, facilitator=fac, fetcher=reader)

    @contextlib.asynccontextmanager
    async def lifespan(_app):  # what the co-host does for the mounted tool
        if web.state.mcp is None:
            yield
            return
        async with web.state.mcp.session_manager.run():
            yield

    host = Starlette(routes=[Mount("/", app=web)], lifespan=lifespan)
    with TestClient(host) as c:
        yield c, web


def _rpc(c: TestClient, method: str, params: dict[str, Any] | None = None, id_: int = 1) -> Any:
    message = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    r = c.post("/mcp/", headers=HEADERS, json=message)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    return body["result"]


def _init(c: TestClient) -> dict[str, Any]:
    return _rpc(c, "initialize", {
        "protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "test-agent", "version": "1"},
    })  # fmt: skip


def _call(
    c: TestClient, args: dict[str, Any], meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": "read_page", "arguments": args}
    if meta:
        params["_meta"] = meta
    return _rpc(c, "tools/call", params, id_=2)


def _payment_required(result: dict[str, Any]) -> PaymentRequired:
    sc = result.get("structuredContent")
    data = (
        sc if isinstance(sc, dict) and "accepts" in sc else json.loads(result["content"][0]["text"])
    )
    return PaymentRequired.model_validate(data)


def _pay(required: PaymentRequired, account: Any = None) -> dict[str, Any]:
    buyer = x402ClientSync()
    buyer.register(required.accepts[0].network, ExactEvmScheme(signer=account or Account.create()))
    payload = buyer.create_payment_payload(required)
    return {MCP_PAYMENT_META_KEY: payload.model_dump(by_alias=True, exclude_none=True)}


def _stats(c: TestClient) -> dict[str, Any]:
    return c.get("/v1/operator/stats", headers={"Authorization": "Bearer op-token"}).json()


def test_nothing_is_mounted_by_default(tmp_path):
    with _server(tmp_path, FakeFacilitator(), mcp=False) as (c, web):
        assert web.state.mcp is None
        assert c.post("/mcp/", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1,
                      "method": "initialize", "params": {}}).status_code == 404  # fmt: skip


def test_the_tool_is_listed_with_its_input_schema(tmp_path):
    with _server(tmp_path, FakeFacilitator()) as (c, _):
        init = _init(c)
        assert init["serverInfo"]["name"] == "inferrail-web-read"
        (tool,) = _rpc(c, "tools/list")["tools"]
        assert tool["name"] == "read_page"
        assert "url" in tool["inputSchema"]["properties"]
        assert tool["inputSchema"]["required"] == ["url"]


def test_an_unpaid_call_gets_the_same_offer_as_the_http_route(tmp_path):
    fac = FakeFacilitator()
    with _server(tmp_path, fac) as (c, _):
        _init(c)
        result = _call(c, {"url": "https://news.test/a"})
        assert result["isError"] is True
        (accept,) = _payment_required(result).accepts
        assert (accept.network, accept.pay_to, accept.amount) == (svc.TESTNET, PAY_TO, "2000")
        assert fac.calls_to("settle") == []
        assert _stats(c)["settled_reads"] == 0


def test_a_paid_call_delivers_settles_once_and_is_recorded(tmp_path):
    fac = FakeFacilitator()
    with _server(tmp_path, fac) as (c, _):
        _init(c)
        required = _payment_required(_call(c, {"url": "https://news.test/a"}))
        result = _call(c, {"url": "https://news.test/a", "request_id": "r1"}, _pay(required))
        assert result.get("isError") in (False, None), result
        page = result["structuredContent"]
        assert "# Hello" in page["content_markdown"]
        assert page["receipt"]["price_usd"] == "0.002" and page["receipt"]["request_id"] == "r1"
        assert MCP_PAYMENT_RESPONSE_META_KEY in (result.get("_meta") or {})
        assert len(fac.calls_to("settle")) == 1
        stats = _stats(c)
        assert stats["settled_reads"] == 1 and stats["revenue_usd"] == "0.002"


def test_the_same_payment_twice_is_never_fetched_or_settled_twice(tmp_path):
    fac = FakeFacilitator()
    with _server(tmp_path, fac) as (c, _):
        _init(c)
        required = _payment_required(_call(c, {"url": "https://news.test/a"}))
        meta = _pay(required)
        first = _call(c, {"url": "https://news.test/a"}, meta)
        assert first.get("isError") in (False, None)
        _call(c, {"url": "https://news.test/a"}, meta)  # replayed payment
        assert len(fac.settled_nonces) == 1
        assert _stats(c)["settled_reads"] == 1


def test_a_refused_page_cancels_the_payment(tmp_path):
    fac = FakeFacilitator()
    with _server(tmp_path, fac) as (c, _):
        _init(c)
        required = _payment_required(_call(c, {"url": "https://news.test/missing"}))
        result = _call(c, {"url": "https://news.test/missing"}, _pay(required))
        assert result["isError"] is True
        assert fac.calls_to("settle") == []
        assert _stats(c)["settled_reads"] == 0
