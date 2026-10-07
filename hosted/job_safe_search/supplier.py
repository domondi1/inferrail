"""One bounded Exa search. No retry or fallback after dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .contract import SearchHit, SearchRequest, atomic


@dataclass
class SupplierResult:
    hits: list[dict[str, Any]]
    cogs: int | None
    provider_request_id: str | None = None


class ExaSearch:
    name = "exa"
    # auto/fast, <=5 results, no summaries/contents/livecrawl: bounded operator contract.
    max_cost = 7000

    def __init__(self, key: str):
        self.key = key
        self.client = httpx.AsyncClient(timeout=25)

    async def search(self, request: SearchRequest) -> SupplierResult:
        response = await self.client.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": self.key},
            json={"query": request.query, "numResults": request.num_results, "type": "auto"},
        )
        # Even HTTP errors or connection timeouts can have charged. Caller freezes ambiguity.
        response.raise_for_status()
        body = response.json()
        hits = [
            SearchHit(
                title=item.get("title") or "", url=item["url"], snippet=item.get("text") or ""
            ).model_dump()
            for item in body["results"][: request.num_results]
        ]
        if not hits:
            raise ValueError("empty_supplier_result")
        cost = body.get("costDollars", {}).get("total")
        cogs = atomic(str(cost)) if cost is not None else None
        if cogs is not None and cogs > self.max_cost:
            raise ValueError("supplier_cost_exceeded_bound")
        return SupplierResult(hits, cogs, body.get("requestId"))


class FixtureSearch:
    """No real supplier or supplier credits; permitted only on testnet."""

    name = "fixture"
    max_cost = 0

    async def search(self, request: SearchRequest) -> SupplierResult:
        return SupplierResult(
            [{"title": "Fixture result", "url": "https://example.com/", "snippet": request.query}],
            0,
            "fixture",
        )


class SerpexSearch:
    """One plain search at the verified paid-credit unit cost; no content add-ons."""

    name = "serpex"

    def __init__(self, key: str, credit_cost: int):
        if not 0 < credit_cost <= 800:
            raise ValueError("verified_paid_credit_cost_must_be_within_800_atomic_USD")
        self.max_cost = credit_cost
        self.client = httpx.AsyncClient(timeout=40)
        self.key = key

    async def search(self, request: SearchRequest) -> SupplierResult:
        response = await self.client.post(
            "https://api.serpex.dev/api/search",
            headers={"Authorization": "Bearer " + self.key},
            json={"q": request.query, "include_content": False},
        )
        response.raise_for_status()
        body = response.json()
        metadata = body.get("metadata", {})
        credits = metadata.get("credits_used")
        # Missing billing evidence stays unknown, even on successful output.
        if credits is not None and (type(credits) is not int or credits not in (0, 1)):
            raise ValueError("supplier_billing_outside_plain_search_contract")
        cogs = credits * self.max_cost if credits is not None else None
        hits = [
            SearchHit(
                title=item["title"], url=item["url"], snippet=item.get("snippet") or ""
            ).model_dump()
            for item in body["results"][: request.num_results]
        ]
        return SupplierResult(hits, cogs, body.get("id"))
