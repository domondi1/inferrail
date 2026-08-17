"""Tests for the inferrail-mcp adapter.

No external network or credentials required — `get_health`'s HTTP call is
mocked via `httpx.MockTransport`, and `get_spend` reads only local fixture
files, consistent with the rest of this suite (see pyproject.toml's
`integration` marker for the one test that genuinely needs a real
provider). Also includes one real, subprocess-based stdio session against
the actual `inferrail-mcp` console script, per this repo's practice of
proving CLI-adjacent behavior end to end (see test_cli_demo.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from inferrail_mcp.server import _get_health_impl, get_spend, server


def _write_receipts(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row))
            f.write("\n")


_RECEIPT_A = {
    "receipt_id": "ir_a",
    "request_id": "req_a",
    "timestamp": "2026-08-16T10:00:00Z",
    "route": "default",
    "provider": "openai",
    "model": "gpt-4o-mini",
    "status": "success",
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "pricing": {
        "input_usd_per_million": "0.15",
        "output_usd_per_million": "0.60",
        "source": "test",
        "verified_date": "2026-08-16",
    },
    "estimated_cost_usd": "0.000045",
    "attributes": {"customer": "acme"},
    "total_latency_ms": 12.0,
    "retry_count": 0,
}

_RECEIPT_B = {
    **_RECEIPT_A,
    "receipt_id": "ir_b",
    "request_id": "req_b",
    "timestamp": "2026-08-17T10:00:00Z",
    "attributes": {"customer": "globex"},
    "estimated_cost_usd": "0.000090",
    "prompt_tokens": 200,
    "completion_tokens": 100,
}


# ---------------------------------------------------------------------------
# get_spend
# ---------------------------------------------------------------------------


def test_get_spend_missing_file_reports_no_requests_not_an_error_field(tmp_path: Path) -> None:
    result = get_spend(receipts_path=str(tmp_path / "nope.jsonl"))
    assert result["groups"] == []
    assert result["error"] == "receipts_file_not_found"
    assert result["schema_version"] == "1"


def test_get_spend_aggregates_by_attribute(tmp_path: Path) -> None:
    path = tmp_path / "receipts.jsonl"
    _write_receipts(path, [_RECEIPT_A, _RECEIPT_B])

    result = get_spend(by="customer", receipts_path=str(path))

    keys = {g["key"] for g in result["groups"]}
    assert keys == {"acme", "globex"}
    acme = next(g for g in result["groups"] if g["key"] == "acme")
    assert acme["known_cost_usd"] == "0.000045"  # a string, never a float
    assert acme["requests"] == 1


def test_get_spend_time_window_filters_receipts(tmp_path: Path) -> None:
    path = tmp_path / "receipts.jsonl"
    _write_receipts(path, [_RECEIPT_A, _RECEIPT_B])

    result = get_spend(by="customer", receipts_path=str(path), since="2026-08-17T00:00:00Z")

    keys = {g["key"] for g in result["groups"]}
    assert keys == {"globex"}  # only receipt B falls after `since`


# ---------------------------------------------------------------------------
# get_health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_health_reachable_true_on_200(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await _get_health_impl(
        "http://127.0.0.1:8000", str(tmp_path / "nope.jsonl"), client=client
    )

    assert result["gateway_reachable"] is True
    assert result["error"] is None
    assert result["health_latency_ms"] is not None


@pytest.mark.asyncio
async def test_get_health_reachable_false_on_connection_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await _get_health_impl(
        "http://127.0.0.1:8000", str(tmp_path / "nope.jsonl"), client=client
    )

    assert result["gateway_reachable"] is False
    assert "unreachable" in (result["error"] or "")


@pytest.mark.asyncio
async def test_get_health_surfaces_most_recent_receipt(tmp_path: Path) -> None:
    path = tmp_path / "receipts.jsonl"
    _write_receipts(path, [_RECEIPT_A, _RECEIPT_B])  # B is the later timestamp

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await _get_health_impl("http://127.0.0.1:8000", str(path), client=client)

    assert result["last_receipt"]["receipt_id"] == "ir_b"


@pytest.mark.asyncio
async def test_get_health_no_receipts_file_is_null_not_an_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await _get_health_impl(
        "http://127.0.0.1:8000", str(tmp_path / "nope.jsonl"), client=client
    )

    assert result["last_receipt"] is None


# ---------------------------------------------------------------------------
# Real end-to-end stdio session, no mocking, against the actual console script
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_stdio_session_lists_and_calls_both_tools(tmp_path: Path) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    receipts_path = tmp_path / "receipts.jsonl"
    _write_receipts(receipts_path, [_RECEIPT_A])

    params = StdioServerParameters(command="inferrail-mcp", args=[])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"get_spend", "get_health"}

            result = await session.call_tool(
                "get_spend", {"by": "customer", "receipts_path": str(receipts_path)}
            )
            assert result.structured_content["groups"][0]["key"] == "acme"


def test_server_name_and_instructions_are_set() -> None:
    # Agent-facing metadata is part of the product surface (see
    # standards/machine-readability.md) — a regression here silently
    # degrades what an agent sees in tools/list, with no test failure
    # anywhere else to catch it.
    assert server.name == "inferrail"
    assert server.instructions is not None
    assert "read-only" in server.instructions
