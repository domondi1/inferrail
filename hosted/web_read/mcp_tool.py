"""Web Read as a paid MCP tool: `read_page`, over MCP streamable HTTP (stateless, JSON).

Off unless `READ_MCP=1`. Payment uses the x402 MCP transport: a call without payment gets a
tool error carrying the x402 PaymentRequired (the same networks, receiving addresses and price
as the HTTP route); an x402 MCP client signs and repeats the call with the payment in
`_meta["x402/payment"]`. The SDK's payment wrapper verifies it, runs the tool, and settles only
when the tool succeeded; a refused or failed page cancels the payment, so it costs nothing.

Accounting is Web Read's own: the same x402 resource server (its after-settle hook marks the
ledger row settled), the same ledger and commerce events, and the same delivery code. The ledger
key is the payment's (EVM `payer:nonce`, Solana `payer:svm:<message hash>`), and one payment is
delivered at most once: presenting it again returns the stored result without fetching again.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

TOOL_NAME = "read_page"
# Matches mcp/web-read/server.json (the MCP Registry entry for this remote server).
SERVER_VERSION = "1.2.2"
TOOL_DESCRIPTION = (
    "Read one public web page as clean, LLM-ready markdown (title, main text, headings, lists, "
    "links, tables) with the page's metadata (description, canonical URL, authors, published "
    "date, language), word count and a content hash. Paid per delivered page over x402; a "
    "blocked, missing, failing or refused page (including a bot check or a page with no text "
    "without JavaScript) costs nothing."
)


def build(
    *,
    settings: Any,
    facilitator: Any,
    make_server: Callable[[Any], Any],
    task: Any,
    ledger: Any,
    rec: Callable[..., None],
    payment_ref: Callable[[Any], tuple[str, str] | None],
    paid_network: Callable[[Any, str], str],
    commerce_events: Any,
) -> Any:
    """The MCPServer (call `.streamable_http_app(...)` and run `.session_manager`)."""
    from mcp.server.mcpserver import Context, MCPServer
    from mcp.types import CallToolResult, TextContent
    from x402.extensions.bazaar import (
        DeclareMcpDiscoveryConfig,
        OutputConfig,
        declare_mcp_discovery_extension,
    )
    from x402.http.types import PaymentOption
    from x402.mcp import MCPToolResult
    from x402.mcp.server_async import PaymentWrapperConfig, create_payment_wrapper
    from x402.mcp.utils import extract_payment_from_meta
    from x402.schemas import ResourceInfo

    # Its own resource server (same schemes and settle accounting as the HTTP route) over a
    # facilitator adapter: the MCP payment wrapper turns any facilitator exception into an opaque
    # "Internal Server Error", while the CDP facilitator answers an invalid payment with HTTP 400.
    schema = task.request_model.model_json_schema()
    example = task.output_example(settings.price_usd)
    resource = ResourceInfo(
        url=f"mcp://tool/{TOOL_NAME}",
        description=TOOL_DESCRIPTION,
        mime_type="application/json",
        service_name=task.service_name,
        tags=list(task.tags)[:5],
        icon_url=settings.icon_url,
    )
    server = make_server(McpFacilitator(facilitator, ledger), resource)
    extensions = declare_mcp_discovery_extension(
        DeclareMcpDiscoveryConfig(
            tool_name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            input_schema=schema,
            example=task.input_example,
            output=OutputConfig(example=example),
        )
    )
    state: dict[str, Any] = {}
    lock = asyncio.Lock()

    async def paid() -> Any:
        """The payment-wrapped handler, built once the facilitator's support is known."""
        async with lock:
            if "wrapped" not in state:
                if not getattr(server, "_initialized", False):
                    await asyncio.to_thread(server.initialize)
                accepts = []
                for network, pay_to in settings.accepted():
                    accepts += server.build_payment_requirements(
                        PaymentOption(
                            scheme="exact",
                            pay_to=pay_to,
                            price=f"${settings.price_usd}",
                            network=network,
                        )  # fmt: skip
                    )
                wrap = create_payment_wrapper(
                    server,
                    PaymentWrapperConfig(accepts=accepts, resource=resource, extensions=extensions),
                )
                state["wrapped"] = wrap(deliver)
        return state["wrapped"]

    async def deliver(args: dict[str, Any], context: Any) -> MCPToolResult:
        payload = extract_payment_from_meta(
            {"name": TOOL_NAME, "arguments": args, "_meta": getattr(context, "meta", {}) or {}}
        )
        ref = payment_ref(payload) if payload is not None else None
        if ref is None:  # the wrapper only calls us after a verified payment; be strict anyway
            return _error({"error": "payment_unreadable", "charged": False})
        used, stored = ledger.result_for(ref[0])
        if used:  # the same payment again: never fetched or charged twice
            if stored:
                return _ok(json.loads(stored))
            return _error({"error": "payment_already_used", "charged": False})
        try:
            body = task.request_model(**args)
        except Exception:
            return _error({"error": "invalid_request", "charged": False})
        mine = {
            "network": paid_network(payload, settings.network),
            "purchase_id": commerce_events.purchase_id("ledger", ref[0]),
            "ledger_key": ref[0], "payer": ref[1], "client": "mcp",
            "request_key": commerce_events.request_key(getattr(body, "request_id", None)),
        }  # fmt: skip
        rec("payment_verified", "x402", **mine)
        rec("delivery_started", "x402", **mine)
        status, row = await task.deliver(body, ledger.event)
        if status != 200:
            rec("delivery_failed", "x402", code=f"http_{status}", **mine)
            return _error(row)
        row["receipt"] = {
            "payment_ref": ref[0], "price_usd": settings.price_usd,
            "request_id": getattr(body, "request_id", None),
        }  # fmt: skip
        ledger.delivered(
            ref[0], ref[1], task.ledger_fields(row), settings.price_usd, task.idem(body),
            json.dumps(row),
        )  # fmt: skip
        rec("delivered", "x402", amount_atomic=int(float(settings.price_usd) * 1_000_000),
            asset="USDC", **mine)  # fmt: skip
        return _ok(row)

    def _ok(row: dict[str, Any]) -> MCPToolResult:
        return MCPToolResult(content=[{"type": "text", "text": json.dumps(row)}],
                             structured_content=row)  # fmt: skip

    def _error(row: dict[str, Any]) -> MCPToolResult:
        return MCPToolResult(content=[{"type": "text", "text": json.dumps(row)}], is_error=True,
                             structured_content=row)  # fmt: skip

    mcp = MCPServer(
        name="inferrail-web-read",
        title="Inferrail Web Read",
        description=TOOL_DESCRIPTION,
        website_url="https://tryinferrail.com",
        version=SERVER_VERSION,
    )

    async def read_page(url: str, ctx: Context, request_id: str | None = None) -> CallToolResult:
        """Read one public web page as clean markdown. Paid per delivered page over x402."""
        meta_obj = ctx.request_context.meta
        if hasattr(meta_obj, "model_dump"):
            meta = meta_obj.model_dump(by_alias=True, exclude_none=True)
        else:  # MCP 2.x hands `_meta` over as a plain mapping
            meta = dict(meta_obj or {})
        args = {"url": url, **({"request_id": request_id} if request_id else {})}
        wrapped = await paid()
        result = await wrapped(args, {"_meta": meta, "toolName": TOOL_NAME})
        return CallToolResult(
            content=[TextContent(type="text", text=c.get("text", "")) for c in result.content],
            isError=bool(result.is_error),
            structuredContent=result.structured_content,
            _meta=result.meta or None,
        )

    # Real types, not postponed strings: MCPServer reads them to build the tool's input schema
    # and to inject the request context (which carries the x402 payment in `_meta`).
    read_page.__annotations__ = {
        "url": str, "ctx": Context, "request_id": str | None, "return": CallToolResult,
    }  # fmt: skip
    mcp.add_tool(read_page, name=TOOL_NAME, description=TOOL_DESCRIPTION)
    return mcp


_FACILITATOR_ERROR = re.compile(r"Facilitator (verify|settle) failed \((\d{3})\): (.*)", re.S)


def _facilitator_answer(error: Exception) -> tuple[int, dict[str, Any]] | None:
    """(HTTP status, JSON body) from the SDK's facilitator-error message, or None."""
    m = _FACILITATOR_ERROR.match(str(error))
    if not m:
        return None
    try:
        body = json.loads(m.group(3))
    except ValueError:
        body = {}
    return int(m.group(2)), body if isinstance(body, dict) else {}


class McpFacilitator:
    """The configured facilitator, with its error answers returned as results (MCP only).

    A 4xx answer to verify becomes an invalid VerifyResponse carrying the facilitator's own
    reason, and to settle a failed SettleResponse, so the buyer gets the x402 PaymentRequired
    or settlement-failed result instead of an opaque error. 5xx and transport errors become
    `facilitator_unavailable` the same way. Nothing is ever settled by this adapter's choice:
    it only reports. Each verify is also counted in the ledger's events (client `mcp`)."""

    def __init__(self, inner: Any, ledger: Any) -> None:
        self._inner = inner
        self._ledger = ledger

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_supported(self) -> Any:
        return self._inner.get_supported()

    def _count(self, kind: str, code: str = "") -> None:
        try:
            self._ledger.event(kind, code)
        except Exception:  # noqa: BLE001 - counting never affects a payment
            pass

    async def verify(self, payload: Any, requirements: Any) -> Any:
        from x402.schemas import VerifyResponse

        self._count("payment_attempt", "mcp")
        try:
            result = await self._inner.verify(payload, requirements)
        except Exception as error:  # noqa: BLE001
            status, body = _facilitator_answer(error) or (0, {})
            reason = body.get("invalidReason") if 400 <= status < 500 else None
            result = VerifyResponse(
                is_valid=False,
                invalid_reason=str(reason or "facilitator_unavailable"),
                invalid_message=str(body.get("invalidMessage") or "")[:200] or None,
                payer=body.get("payer"),
            )
        if not getattr(result, "is_valid", False):
            self._count("payment_rejected", "mcp")
            self._count("attempt_failed", f"mcp:{getattr(result, 'invalid_reason', '')}"[:60])
        return result

    async def settle(self, payload: Any, requirements: Any) -> Any:
        from x402.schemas.responses import SettleResponse

        try:
            return await self._inner.settle(payload, requirements)
        except Exception as error:  # noqa: BLE001
            status, body = _facilitator_answer(error) or (0, {})
            reason = body.get("errorReason") if 400 <= status < 500 else None
            self._count("attempt_failed", f"mcp:settle:{reason or 'facilitator_unavailable'}"[:60])
            return SettleResponse(
                success=False,
                error_reason=str(reason or "facilitator_unavailable"),
                transaction="",
                network=str(getattr(requirements, "network", "")),
                payer=body.get("payer"),
            )
