"""Public request and result contract; money uses atomic USDC units."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def atomic(value: str) -> int:
    amount = Decimal(value)
    if (
        not amount.is_finite()
        or amount < 0
        or amount * 1_000_000 != (amount * 1_000_000).to_integral()
    ):
        raise ValueError("amount must be a nonnegative decimal with at most six places")
    return int(amount * 1_000_000)


def usd(value: int | None) -> str | None:
    return None if value is None else format(Decimal(value) / 1_000_000, "f")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=2000)
    num_results: int = Field(default=5, ge=1, le=5)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    job_id: str | None = Field(default=None, min_length=1, max_length=128)
    job_token: str | None = Field(default=None, max_length=2048)
    job_budget_usd: str | None = Field(default=None, max_length=32)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("query must contain text")
        return value

    @field_validator("job_budget_usd")
    @classmethod
    def budget(cls, value: str | None) -> str | None:
        if value is not None:
            atomic(value)
        return value

    def fingerprint(self) -> str:
        canonical = json.dumps(
            {"query": self.query, "num_results": self.num_results}, sort_keys=True
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str
    url: str
    snippet: str = ""

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("result URL must be HTTP(S)")
        return value


INPUT_EXAMPLE: dict[str, Any] = {
    "query": "current AI inference prices",
    "num_results": 5,
    "request_id": "research-001",
    "job_budget_usd": "0.045",
}
DESCRIPTION = (
    "Job-safe web search for agents: up to 5 ranked titles, URLs and snippets. "
    "Upfront USDC buys one bounded attempt. Stable request_id prevents duplicate payment; "
    "job_token enables a fixed job budget and free 5-minute same-job cache. "
    "Poll HTTP 202 using the original signature/token; never pay again for that request. "
    "Failed attempts may remain charged; no automatic refunds. EOA EIP-3009 only."
)

OUTPUT_EXAMPLE: dict[str, Any] = {
    "results": [
        {
            "title": "Example result",
            "url": "https://example.com/",
            "snippet": "A short relevant excerpt.",
        }
    ],
    "job_id": "opaque-job-id",
    "job_token": "opaque-job-capability",
    "receipt": {
        "receipt_id": "search-1",
        "request_id": "research-001",
        "charged_usd": "0.010",
        "provider": "serpex",
        "cache_hit": False,
        "replayed": False,
        "failover": False,
        "remaining_job_budget_usd": "0.035",
        "economic_state": "SETTLED",
        "transaction": "0x...",
        "financial_state": "RESOLVED",
        "result_count": 1,
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string", "format": "uri"},
                    "snippet": {"type": "string"},
                },
                "required": ["title", "url", "snippet"],
            },
        },
        "job_id": {"type": "string"},
        "job_token": {"type": "string"},
        "receipt": {
            "type": "object",
            "properties": {
                "receipt_id": {"type": "string"},
                "request_id": {"type": "string"},
                "charged_usd": {"type": ["string", "null"]},
                "original_charge_usd": {"type": ["string", "null"]},
                "provider": {"type": "string"},
                "cache_hit": {"type": "boolean"},
                "replayed": {"type": "boolean"},
                "failover": {"type": "boolean"},
                "remaining_job_budget_usd": {"type": ["string", "null"]},
                "economic_state": {"type": "string"},
                "transaction": {"type": ["string", "null"]},
                "financial_state": {"type": "string"},
                "result_count": {"type": "integer"},
            },
            "required": [
                "receipt_id",
                "request_id",
                "charged_usd",
                "provider",
                "cache_hit",
                "replayed",
                "failover",
                "remaining_job_budget_usd",
                "economic_state",
                "transaction",
                "financial_state",
            ],
        },
    },
}
