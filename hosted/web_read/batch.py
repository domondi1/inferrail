"""Inferrail Web Read, several pages in one paid request.

`POST /read/v1/pages {"urls": ["https://...", ...], "request_id": "..."}` reads 1 to 10 public
pages, each exactly as `POST /read/v1/page` would (same fetcher, robots.txt, address checks,
size and time limits, markdown and metadata), and returns them in the order given.

Price: $0.002 per page, so a request for N URLs is offered (x402 exact and MPP on Tempo) at
N x $0.002, and that is what settles when it is delivered. **All or nothing**: the batch is
delivered only when every page is; if any page fails, nothing is settled and no page content is
returned, only which URLs failed and why, so the buyer can send again without them. One payment
buys one batch: a retry with the same URLs (same order) and request_id from the same wallet
within the recovery window returns the paid batch without a second charge.

It is a Web Read task (see service.Task) priced per unit (`units`: one per URL), so every offer,
signature check, receipt and settlement is for the batch's exact amount.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BaseModel, Field, WithJsonSchema, field_validator

MAX_URLS = 10
CONCURRENCY = 4  # pages fetched at once per batch
DEADLINE_S = 45.0  # the whole batch; each page also has the fetcher's own timeout
MAX_TOTAL_CHARS = 4_000_000  # markdown across the batch; beyond it the batch is refused (free)

DESCRIPTION = (  # echoed to the facilitator: kept under service.OFFER_DESCRIPTION_MAX
    "Web pages to Markdown in bulk: read up to 10 public URLs in one call as clean, LLM-ready "
    "Markdown with each page's metadata, in the order given. $0.002 per page in one payment. All "
    "or nothing: if any page fails, nothing is charged and the answer lists which URLs failed."
)
MPP_DESCRIPTION = (
    "Up to 10 public web pages as clean markdown in one call, $0.002 per page. Charged only "
    "when every page is delivered; a retry never pays twice."
)
SUMMARY = "Read up to 10 web pages as markdown in one paid call ($0.002 per page)"
TAGS = ["web pages to markdown", "batch url to markdown", "web scraper", "read urls", "bulk"]


class PagesRequest(BaseModel):
    urls: Annotated[
        list[str],
        WithJsonSchema({"type": "array", "minItems": 1, "maxItems": MAX_URLS,
                        "items": {"type": "string", "maxLength": 2048},
                        "description": "Public http(s) URLs to read (1-10, no duplicates)"}),
    ] = Field(..., min_length=1, max_length=MAX_URLS)  # fmt: skip
    request_id: Annotated[str | None, WithJsonSchema({"type": "string", "maxLength": 128})] = Field(
        None,
        max_length=128,
        description="Your id, echoed back. Retrying with the same urls and request_id within the "
        "recovery window returns the paid batch without a second charge.",
    )

    @field_validator("urls")
    @classmethod
    def _urls(cls, urls: list[str]) -> list[str]:
        cleaned = [u.strip() for u in urls]
        if any(not u or len(u) > 2048 for u in cleaned):
            raise ValueError("each url must be 1-2048 characters")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate urls: list each page once")
        return cleaned


PAGE_SCHEMA_KEYS = ("url", "final_url", "title", "content_markdown", "word_count",
                    "content_sha256", "fetched_at", "metadata")  # fmt: skip


def output_schema(page_schema: dict[str, Any]) -> dict[str, Any]:
    page = {"type": "object", "properties": {k: v for k, v in page_schema["properties"].items()
                                             if k in PAGE_SCHEMA_KEYS}}  # fmt: skip
    return {
        "type": "object",
        "properties": {
            "pages": {"type": "array", "items": page},
            "count": {"type": "integer"},
            "content_sha256": {"type": "string"},
            "receipt": {
                "type": "object",
                "properties": {"payment_ref": {"type": "string"}, "price_usd": {"type": "string"},
                               "request_id": {"type": "string"}},
            },
        },
        "required": ["pages", "count", "content_sha256"],
    }  # fmt: skip


def batch_task(service: Any, reader: Any) -> Any:
    """The batch task for `service.create_app` (`service` is the loaded Web Read module)."""

    async def deliver(
        body: PagesRequest, event: Callable[[str, str], None]
    ) -> tuple[int, dict[str, Any]]:
        sem = asyncio.Semaphore(CONCURRENCY)
        silent: list[tuple[str, str]] = []  # per-page refusals, recorded once for the batch

        async def one(url: str) -> tuple[int, dict[str, Any]]:
            async with sem:
                status, row = await service.read_page(
                    reader, url, lambda k, c: silent.append((k, c))
                )
                return int(status), dict(row)

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(one(u) for u in body.urls)), DEADLINE_S
            )
        except TimeoutError:
            event("refused", "batch_timeout")
            return 504, {"error": "timeout", "detail": "the batch did not finish in time",
                         "charged": False}  # fmt: skip
        failed = [(u, s, r) for u, (s, r) in zip(body.urls, results, strict=True) if s != 200]
        if failed:
            event("refused", "batch_incomplete")
            # Retrying the same batch can only help if every failure may pass later.
            status = 502 if all(s in (502, 504) for _, s, _ in failed) else 422
            return status, {
                "error": "pages_failed",
                "detail": "not every page could be delivered, so nothing was charged; send "
                "again without the failed urls",
                "charged": False,
                "results": [
                    {"url": u, "delivered": s == 200}
                    | ({"error": r.get("error"), "detail": r.get("detail")} if s != 200 else {})
                    for u, (s, r) in zip(body.urls, results, strict=True)
                ],
            }
        pages = [row for _, row in results]
        if sum(len(p["content_markdown"]) for p in pages) > MAX_TOTAL_CHARS:
            event("refused", "batch_too_large")
            return 422, {"error": "too_large", "detail": "the batch's text is over the limit; "
                         "send fewer pages", "charged": False}  # fmt: skip
        joined = "\n".join(p["content_sha256"] for p in pages)
        return 200, {
            "pages": pages,
            "count": len(pages),
            "content_sha256": hashlib.sha256(joined.encode()).hexdigest(),
            "bytes": sum(int(p.get("bytes", 0)) for p in pages),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def idem(body: PagesRequest) -> str:
        # Namespaced, so a batch's retry identity can never equal a single page's.
        raw = "batch\n" + "\n".join(body.urls) + "\n" + (body.request_id or "")
        return hashlib.sha256(raw.encode()).hexdigest()

    def example(price_usd: str) -> dict[str, Any]:
        page = service.page_task(reader).output_example(price_usd)
        page.pop("receipt", None)
        two = service.price_for(price_usd, 2)
        return {"pages": [page, {**page, "url": "https://example.org/",
                                 "final_url": "https://example.org/"}],
                "count": 2, "content_sha256": "9b2e…",
                "receipt": {"payment_ref": "0xpayer:0xnonce", "price_usd": two}}  # fmt: skip

    def ledger_fields(row: dict[str, Any]) -> tuple[Any, ...]:
        words = sum(int(p.get("word_count", 0)) for p in row["pages"])
        return None, None, int(row.get("bytes", 0)), words, row["content_sha256"]

    def guide(settings: Any) -> str:
        return f"# Inferrail Web Read (batch)\n\n> {DESCRIPTION}\n"

    return service.Task(
        path="/read/v1/pages",
        request_model=PagesRequest,
        service_name="Inferrail Web Read",
        listing_name="Web Pages to Markdown, batch",
        description=DESCRIPTION,
        mpp_description=MPP_DESCRIPTION,
        tags=TAGS,
        input_example={"urls": ["https://example.com/", "https://example.org/"]},
        output_example=example,
        output_schema=output_schema(service.OUTPUT_SCHEMA),
        idem=idem,
        deliver=deliver,
        ledger_fields=ledger_fields,
        guide=guide,
        units=lambda body: len(body.urls),
    )
