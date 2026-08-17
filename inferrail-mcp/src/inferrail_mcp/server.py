"""The two Inferrail MCP tools: `get_spend` and `get_health`.

Deliberately narrow — see `inferrail-mcp/README.md` for why
`configure_budget` and `explain_error` (until stable error codes exist)
are not here. Both tools are read-only: neither can execute inference,
spend money, or mutate configuration. `get_spend` wraps the exact
aggregation `inferrail report` already implements
(`inferrail.cli.report.load_receipts`/`aggregate`) over the local
receipts JSONL file — no new execution path. `get_health` pings the
gateway's existing `/health` endpoint and, separately, reports the most
recent local receipt as evidence of attribution — it never makes a new,
billable inference call, so it can't silently spend an operator's
provider budget as a side effect of a "health check."

Every response follows `standards/machine-readability.md` (internal):
explicit units (`known_cost_usd` as a string, never a bare float),
explicit nullability (`null` when unknown, never a fabricated `0`),
stable identifiers, and a `schema_version` on every payload.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from inferrail import __version__
from inferrail.cli.report import ReportGroup, aggregate, load_receipts
from inferrail.receipts.schema import InferenceReceipt

try:
    from mcp.server.mcpserver import MCPServer
except ModuleNotFoundError as exc:  # pragma: no cover - guidance, not logic
    raise ModuleNotFoundError(
        "The 'mcp' package is required to run inferrail-mcp. Install with: "
        "pip install 'inferrail[mcp]'"
    ) from exc

_SCHEMA_VERSION = "1"

server = MCPServer(
    name="inferrail",
    version=__version__,
    instructions=(
        "Inferrail is a self-hosted, OpenAI-compatible LLM gateway that "
        "attributes the cost of every request to caller-supplied business "
        "context (customer, workflow, agent, ...) in a local, payload-free "
        "receipt ledger. These tools are read-only: get_spend queries that "
        "ledger, get_health checks whether the gateway is up and has "
        "recently attributed activity. Neither tool executes inference, "
        "spends provider budget, or changes configuration."
    ),
)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _group_to_dict(group: ReportGroup) -> dict[str, Any]:
    data = asdict(group)
    data["known_cost_usd"] = str(group.known_cost_usd)
    return data


def _receipt_summary(receipt: InferenceReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "timestamp": receipt.timestamp.isoformat(),
        "status": receipt.status,
        "provider": receipt.provider,
        "model": receipt.model,
        "estimated_cost_usd": (
            str(receipt.estimated_cost_usd) if receipt.estimated_cost_usd is not None else None
        ),
    }


@server.tool(
    name="get_spend",
    title="Query attributed spend",
    description=(
        "Query Inferrail's local receipt ledger, aggregated by a dimension "
        "(provider, model, route, or any business attribute name such as "
        "'customer' or 'agent'), optionally restricted to a time window. "
        "Returns known cost in USD (as a decimal string, never a float) "
        "per group, plus a count of requests whose pricing was unresolvable "
        "-- those are never silently folded into the cost total as zero. "
        "Reads a local file only; makes no network call and cannot incur "
        "provider cost."
    ),
)
def get_spend(
    by: str = "customer",
    receipts_path: str = "./inferrail-receipts.jsonl",
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Aggregate local Inferrail receipts by `by`, optionally within [since, until).

    `by`: "provider", "model", "route", or any attribute name (e.g. "customer",
    "workflow", "agent") a caller has attached via `-a KEY=VALUE` or
    `X-Inferrail-Attribute-<Name>`.
    `since`/`until`: ISO 8601 timestamps (e.g. "2026-08-01T00:00:00Z"). Naive
    timestamps are treated as UTC. Both are inclusive-exclusive: `since` <=
    timestamp < `until`.
    """
    path = Path(receipts_path)
    if not path.exists():
        return {
            "schema_version": _SCHEMA_VERSION,
            "by": by,
            "receipts_path": receipts_path,
            "error": "receipts_file_not_found",
            "message": f"No receipts file at {receipts_path}. No requests have been made yet.",
            "groups": [],
            "skipped_malformed_rows": 0,
        }

    receipts, skipped = load_receipts(path)

    since_dt = _parse_timestamp(since) if since else None
    until_dt = _parse_timestamp(until) if until else None
    if since_dt is not None or until_dt is not None:
        receipts = [
            r
            for r in receipts
            if (since_dt is None or r.timestamp >= since_dt)
            and (until_dt is None or r.timestamp < until_dt)
        ]

    groups = aggregate(receipts, by)

    return {
        "schema_version": _SCHEMA_VERSION,
        "by": by,
        "since": since,
        "until": until,
        "receipts_path": receipts_path,
        "groups": [_group_to_dict(g) for g in groups],
        "skipped_malformed_rows": skipped,
    }


@server.tool(
    name="get_health",
    title="Check gateway health and recent attribution",
    description=(
        "Check whether an Inferrail gateway is reachable at base_url via its "
        "/health endpoint, and separately report the most recent local "
        "receipt (if any) as evidence of recent attributed activity. Does "
        "NOT make a new inference call -- a health check must never silently "
        "spend provider budget as a side effect."
    ),
)
async def get_health(
    base_url: str = "http://127.0.0.1:8000",
    receipts_path: str = "./inferrail-receipts.jsonl",
) -> dict[str, Any]:
    """Report gateway reachability and the most recent local receipt, if any."""
    return await _get_health_impl(base_url, receipts_path, client=None)


async def _get_health_impl(
    base_url: str,
    receipts_path: str,
    *,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """The testable implementation behind `get_health`.

    Separated from the `@server.tool`-decorated function above because an
    injectable `client` parameter (mirroring `OpenAIProvider`'s own
    testability pattern) can't be part of the tool's schema — the MCP
    SDK generates that schema from the decorated function's type hints,
    and `httpx.AsyncClient` isn't JSON-Schema-representable. MCP callers
    only ever reach `get_health`, never this function directly.
    """
    checked_at = datetime.now(UTC).isoformat()
    gateway_reachable = False
    health_latency_ms: float | None = None
    error: str | None = None

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=5.0)
    started = time.perf_counter()
    try:
        try:
            response = await http_client.get(f"{base_url.rstrip('/')}/health")
        except httpx.HTTPError as exc:
            error = f"gateway unreachable at {base_url}: {exc}"
        else:
            health_latency_ms = (time.perf_counter() - started) * 1000
            gateway_reachable = response.status_code == 200
            if not gateway_reachable:
                error = f"gateway returned HTTP {response.status_code}"
    finally:
        if owns_client:
            await http_client.aclose()

    last_receipt: dict[str, Any] | None = None
    path = Path(receipts_path)
    if path.exists():
        receipts, _ = load_receipts(path)
        if receipts:
            last_receipt = _receipt_summary(max(receipts, key=lambda r: r.timestamp))

    return {
        "schema_version": _SCHEMA_VERSION,
        "checked_at": checked_at,
        "base_url": base_url,
        "gateway_reachable": gateway_reachable,
        "health_latency_ms": health_latency_ms,
        "error": error,
        "receipts_path": receipts_path,
        "last_receipt": last_receipt,
    }


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
