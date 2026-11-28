"""Inferrail Web Read over HTTPS with a workspace API key (prepaid credits). No Dify imports, so
it can be tested on its own. A delivered page costs 2 credits ($0.002); a page that can't be read
costs nothing."""

from __future__ import annotations

from typing import Any

import httpx

ORIGIN = "https://inferrail-cost-gateway.onrender.com"
READ_PATH = "/read/credits/v1/page"
WORKSPACE_PATH = "/hosted/v1/workspace"
SOURCE = {"x-inferrail-source": "dify"}
TIMEOUT_S = 60.0
MAX_URLS = 10


class InferrailError(Exception):
    """A refusal the user can act on (bad key, no credits). Never contains the key."""


def _headers(api_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {api_key}", "content-type": "application/json", **SOURCE}


def check_key(api_key: str, client: httpx.Client | None = None, origin: str = ORIGIN) -> int:
    """Validate a workspace key for free; return its prepaid credit balance."""
    if not api_key or not api_key.startswith("irw_"):
        raise InferrailError(
            "An Inferrail API key starts with irw_ (create one at POST "
            f"{origin}/hosted/v1/workspaces)."
        )
    with client or httpx.Client(timeout=TIMEOUT_S) as c:
        r = c.get(f"{origin}{WORKSPACE_PATH}", headers=_headers(api_key))
    if r.status_code == 401:
        raise InferrailError("This Inferrail API key is not valid.")
    r.raise_for_status()
    return int(r.json().get("credits_remaining") or 0)


def read_page(
    api_key: str,
    url: str,
    request_id: str | None = None,
    client: httpx.Client | None = None,
    origin: str = ORIGIN,
) -> dict[str, Any]:
    """One page as markdown plus metadata. Returns the service's JSON; `ok` is False with
    `charged: False` when the page couldn't be read. Raises InferrailError for a bad key or too
    few credits (the user has to act)."""
    body: dict[str, Any] = {"url": url}
    if request_id:
        body["request_id"] = request_id
    with client or httpx.Client(timeout=TIMEOUT_S) as c:
        r = c.post(f"{origin}{READ_PATH}", headers=_headers(api_key), json=body)
    data: dict[str, Any] = {}
    try:
        data = r.json()
    except ValueError:
        pass
    if r.status_code == 200:
        return {"ok": True, **data}
    if r.status_code == 401:
        raise InferrailError("This Inferrail API key is not valid.")
    if r.status_code == 402 and data.get("error") == "insufficient_credits":
        top_up = (data.get("top_up") or {}).get("card") or (data.get("top_up") or {}).get("x402")
        raise InferrailError(
            f"Not enough Inferrail credits ({data.get('credits_remaining', 0)} left, "
            f"{data.get('credits_needed', 2)} needed per page). Top up once: {top_up}"
        )
    return {
        "ok": False,
        "charged": False,
        "url": url,
        "status": r.status_code,
        "error": str(data.get("error") or f"http_{r.status_code}"),
        "detail": str(data.get("detail") or "")[:300],
    }


def parse_urls(text: str) -> list[str]:
    """URLs from a newline- or comma-separated string, deduplicated, at most MAX_URLS."""
    seen: list[str] = []
    for part in text.replace(",", "\n").split():
        u = part.strip()
        if u and u not in seen:
            seen.append(u)
    if len(seen) > MAX_URLS:
        raise InferrailError(f"At most {MAX_URLS} URLs per call.")
    return seen


def to_markdown(page: dict[str, Any]) -> str:
    """The page as one markdown document with a short provenance header."""
    meta = page.get("metadata") or {}
    lines = [
        f"# {page.get('title') or page.get('url')}",
        f"Source: {page.get('final_url') or page.get('url')}",
    ]
    for label, key in (
        ("Published", "published_at"),
        ("Authors", "authors"),
        ("Language", "language"),
    ):
        value = meta.get(key)
        if value:
            lines.append(f"{label}: {', '.join(value) if isinstance(value, list) else value}")
    lines += ["", str(page.get("content_markdown") or "")]
    return "\n".join(lines)
