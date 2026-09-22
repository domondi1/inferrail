"""Authentication and per-tenant request-rate limiting for the hosted Cost
Gateway.

Unlike `hosted/ap_exceptions`, there is no operator-provisioned API key
in this service at all -- every tenant is a self-serve trial tenant
minted by `POST /v1/trial` (`trial.py`). This module's only job is to
resolve a bearer token back to its `Tenant` and reject anything
missing/invalid/expired, plus enforce a per-tenant request-rate limit
identical in shape to `hosted/ap_exceptions/auth.py`'s.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from trial import Tenant, TrialRegistry, iso


def authenticate(request: Request, registry: TrialRegistry) -> Tenant:
    """FastAPI-dependency-style helper: validates the `Authorization:
    Bearer <trial-api-key>` header and returns the caller's live
    `Tenant`. Raises 401 for anything missing, malformed, unknown, or
    expired -- never falls back to an unauthenticated default tenant."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="missing or malformed Authorization: Bearer <trial-api-key> header",
        )
    api_key = header[len("bearer ") :].strip()
    tenant = registry.lookup(api_key)
    if tenant is None:
        raise HTTPException(status_code=401, detail="invalid or unknown trial API key")
    if tenant.is_expired():
        raise HTTPException(
            status_code=401,
            detail=(
                f"trial expired at {iso(tenant.expires_at)} -- trials are short-lived by "
                "design; issue a new one with POST /v1/trial"
            ),
        )
    return tenant


class RateLimiter:
    """A simple in-memory, per-tenant fixed-window request counter.
    Resets on process restart -- adequate for a single-instance
    deployment, same as `hosted/ap_exceptions/auth.py`'s identically-
    shaped limiter; a multi-instance deployment would need a shared
    backend instead, not implemented in this release."""

    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    @property
    def max_requests(self) -> int:
        return self._max_requests

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    def check(self, tenant_id: str) -> None:
        now = time.monotonic()
        window = self._requests[tenant_id]
        while window and now - window[0] > self._window_seconds:
            window.popleft()
        if len(window) >= self._max_requests:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"rate limit exceeded: max {self._max_requests} requests per "
                    f"{self._window_seconds:.0f}s per trial"
                ),
            )
        window.append(now)


def rate_limiter_from_env() -> RateLimiter:
    import os

    return RateLimiter(
        max_requests=int(os.environ.get("COST_GATEWAY_RATE_LIMIT_MAX_REQUESTS", "60")),
        window_seconds=float(os.environ.get("COST_GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "60")),
    )
