"""Inferrail Web Search: paid web search for agents, sold through Web Read's payment flow.

`POST /v1/web {"query": ...}` returns ranked title, URL and snippet results with a content
hash and a receipt. It is a Web Read task (see service.Task): the same x402/MPP offers,
validate → deliver → settle ordering, recovery of a paid result without a second charge, and
funnel. **Charged only when results are delivered:** a failed or empty supplier search costs
the buyer nothing.

Results come from the supplier adapter in hosted/job_safe_search/supplier.py (Serpex by
default, plain search, no content add-ons). Nothing here spends money by itself: the adapter
needs the supplier key, and service.Settings needs explicit approvals to sell on mainnet.

**The query is never recorded.** Receipts, the ledger and the recovery copy hold results,
counts and hashes only. A retry is recognised by an HMAC of (query, num_results, request_id)
under a service secret, so the ledger can't be searched for what someone asked.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import importlib.util
import json
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field, WithJsonSchema, field_validator

SUPPLIER_DIR = Path(__file__).resolve().parent.parent / "job_safe_search"
PACKAGE = "inferrail_job_safe_search"

DESCRIPTION = (
    "Search the web: ranked results (title, URL, snippet) for a query, with a content hash and "
    "a receipt. Pay only for delivered results: a failed or empty search costs nothing. A retry "
    "with the same query and request_id from the same wallet never pays twice."
)
MPP_DESCRIPTION = (
    "Web search results (title, URL, snippet) for one query, with a content hash and receipt. "
    "Charged only when results are delivered; a retry never pays twice."
)
SUMMARY = "Web search: ranked title, URL and snippet results for a query"
TAGS = ["web search", "search the web", "search results", "serp", "search api"]

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "snippet": {"type": "string"},
                },
                "required": ["title", "url", "snippet"],
            },
        },
        "result_count": {"type": "integer"},
        "results_sha256": {"type": "string"},
        "searched_at": {"type": "string"},
        "receipt": {
            "type": "object",
            "properties": {
                "payment_ref": {"type": "string"},
                "price_usd": {"type": "string"},
                "request_id": {"type": "string"},
            },
        },
    },
    "required": ["results", "result_count", "results_sha256"],
}


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=400, description="What to search the web for")
    num_results: int = Field(5, ge=1, le=5, description="How many results (1-5)")
    request_id: Annotated[str | None, WithJsonSchema({"type": "string", "maxLength": 128})] = Field(
        None,
        max_length=128,
        description="Your id, echoed back. Retrying with the same query and request_id within the "
        "recovery window returns the paid results without a second charge.",
    )

    @field_validator("query")
    @classmethod
    def _normalize(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("query must contain text")
        return value


def load_suppliers() -> tuple[Any, Any]:
    """(supplier module, contract module) from hosted/job_safe_search, loaded by path like the
    rest of the hosted apps (the production entry point runs as a script, not a package)."""
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, SUPPLIER_DIR / "__init__.py", submodule_search_locations=[str(SUPPLIER_DIR)]
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = package
        spec.loader.exec_module(package)
    return (
        importlib.import_module(f"{PACKAGE}.supplier"),
        importlib.import_module(f"{PACKAGE}.contract"),
    )


def _b64(value: dict[str, Any]) -> str:
    """Unpadded base64url JSON, as MPP encodes a challenge request (used by tests)."""
    import base64

    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sha256(results: list[dict[str, Any]]) -> str:
    canonical = json.dumps(results, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def search_task(service: Any, supplier: Any, secret: bytes) -> Any:
    """The Web Search task for `service.create_app` (`service` is the loaded Web Read service
    module). `supplier` has `async search(request)` (hosted/job_safe_search/supplier.py);
    `secret` keys the retry identity."""
    if len(secret) < 16:
        raise ValueError("search_retry_secret_too_short")
    cost = getattr(supplier, "max_cost", None)  # the verified cost per search, 6-decimal USD
    if type(cost) is not int or cost <= 0:
        raise ValueError("supplier_cost_per_search_required")
    suppliers, contract = load_suppliers()

    def idem(body: SearchRequest) -> str:
        message = f"{body.query}\n{body.num_results}\n{body.request_id or ''}".encode()
        return hmac.new(secret, message, hashlib.sha256).hexdigest()

    async def deliver(
        body: SearchRequest, event: Callable[[str, str], None]
    ) -> tuple[int, dict[str, Any]]:
        request = contract.SearchRequest(
            query=body.query, num_results=body.num_results, request_id="inferrail-web-search"
        )
        try:
            got = await supplier.search(request)
        except suppliers.SupplierFailure as exc:
            event("refused", "supplier_failed")
            if exc.cogs:
                event("supplier_cost", str(exc.cogs))  # a failed attempt can still be billed
            return 502, {"error": "search_failed", "charged": False}
        except Exception:
            event("refused", "supplier_error")
            return 502, {"error": "search_failed", "charged": False}
        if got.cogs is not None:
            event("supplier_cost", str(got.cogs))
        results = [
            {"title": h["title"], "url": h["url"], "snippet": h.get("snippet") or ""}
            for h in got.hits[: body.num_results]
        ]
        return 200, {
            "results": results,
            "result_count": len(results),
            "results_sha256": _sha256(results),
            "searched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def example(price_usd: str) -> dict[str, Any]:
        results = [
            {
                "title": "Example Domain",
                "url": "https://example.com/",
                "snippet": "This domain is for use in documentation examples.",
            }
        ]
        return {
            "results": results,
            "result_count": 1,
            "results_sha256": _sha256(results),
            "searched_at": "2026-11-11T12:00:00Z",
            "receipt": {"payment_ref": "tempo:0xtx", "price_usd": price_usd},
        }

    def guide(settings: Any) -> str:
        rails = []
        if settings.mpp:
            rails.append(
                f"MPP on Tempo (chain {settings.mpp.chain_id}): the 402 carries a "
                "WWW-Authenticate Payment challenge; repeat the request with Authorization: "
                "Payment <credential>. The transfer is broadcast only after results are delivered."
            )
        if settings.x402:
            rails.append(
                f"x402 v2 exact, ${settings.price_usd} USDC on {settings.network} to "
                f"{settings.pay_to}: sign the 402 and repeat with the PAYMENT-SIGNATURE header."
            )
        return (
            "# Inferrail Web Search\n\n"
            f"> {DESCRIPTION}\n\n"
            f'POST {settings.resource_url} with JSON {{"query": "...", "num_results": 5, '
            '"request_id": "..."}.\n'
            f"Price: ${settings.price_usd} per delivered search. The first response is HTTP 402.\n"
            + "".join(f"Pay with {r}\n" for r in rails)
            + "HTTP 200 = delivered and paid. 502 {error: search_failed} = not charged; it may "
            "succeed later. 422 = invalid request, not charged.\n"
            "Lost the response? Send the same query, num_results and request_id again from the "
            f"same wallet within {settings.recovery_s // 60} minutes: you get the paid results "
            "back and are not charged again.\n"
            "Your query is not stored: receipts and records keep results, counts and hashes only.\n"
        )

    return service.Task(
        path="/v1/web",
        request_model=SearchRequest,
        service_name="Inferrail Web Search",
        description=DESCRIPTION,
        mpp_description=MPP_DESCRIPTION,
        tags=["web-search", "search", "serp"],
        input_example={"query": "tempo machine payments protocol", "num_results": 5},
        output_example=example,
        output_schema=OUTPUT_SCHEMA,
        idem=idem,
        deliver=deliver,
        # (url, final_url, bytes, words, sha256): no query, ever
        ledger_fields=lambda row: (
            None,
            None,
            len(json.dumps(row["results"]).encode()),
            row["result_count"],
            row["results_sha256"],
        ),
        guide=guide,
        unit_cost_usd=Decimal(cost) / Decimal(10**6),
    )
