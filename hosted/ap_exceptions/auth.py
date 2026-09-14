"""Authentication and per-tenant request-rate limiting for the hosted
AP exceptions service.

API keys are configured operator-side via the `AP_API_KEYS` environment
variable (comma-separated). There is no self-serve key issuance in this
release -- a key is provisioned by whoever operates this deployment.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from tenant_store import tenant_id_for_api_key


def _configured_api_keys() -> frozenset[str]:
    raw = os.environ.get("AP_API_KEYS", "")
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


def authenticate(request: Request) -> str:
    """FastAPI dependency: validates the `Authorization: Bearer <key>`
    header against `AP_API_KEYS` and returns the caller's tenant id.
    Raises 401 if missing or invalid -- never falls back to an
    unauthenticated default tenant."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="missing or malformed Authorization: Bearer <api-key> header"
        )
    api_key = header[len("bearer ") :].strip()
    valid_keys = _configured_api_keys()
    if not valid_keys:
        raise HTTPException(
            status_code=503,
            detail="server is not configured with any AP_API_KEYS -- refusing all requests",
        )
    if api_key not in valid_keys:
        raise HTTPException(status_code=401, detail="invalid API key")
    return tenant_id_for_api_key(api_key)


class RateLimiter:
    """A simple in-memory, per-tenant fixed-window request counter.
    Resets on process restart -- adequate for a single-instance
    deployment; a multi-instance deployment would need a shared backend
    (e.g. Redis) instead, not implemented in this release."""

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
                    f"{self._window_seconds:.0f}s per API key"
                ),
            )
        window.append(now)


def rate_limiter_from_env() -> RateLimiter:
    return RateLimiter(
        max_requests=int(os.environ.get("AP_RATE_LIMIT_MAX_REQUESTS", "120")),
        window_seconds=float(os.environ.get("AP_RATE_LIMIT_WINDOW_SECONDS", "60")),
    )
