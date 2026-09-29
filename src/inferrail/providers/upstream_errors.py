"""Recognising an upstream's *budget or quota* refusal, as opposed to a
rate limit or an auth failure — see ``errors.UpstreamBudgetExceededError``.

Matches categorical fields first (the HTTP status 402, or ``error.type`` /
``error.code`` values gateways and providers document). Some gateways send
only a free-text ``detail`` (otari: "API key has exceeded budget limit"),
so a 403/429 whose text mentions a budget also counts. That text is read
only to classify; it is never stored.
"""

from __future__ import annotations

import httpx

_BUDGET_TYPES = frozenset(
    {
        "budget_exceeded",  # LiteLLM
        "quota_for_entity_exceeded",  # Vercel AI Gateway
        "insufficient_quota",  # OpenAI
    }
)


def upstream_budget_refusal_type(response: httpx.Response) -> str | None:
    """The upstream's own label for a budget/quota refusal, or ``None``."""
    status = response.status_code
    try:
        data = response.json()
    except ValueError:
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    fields: list[str] = []
    if isinstance(error, dict):
        fields = [str(error.get(k)) for k in ("type", "code") if error.get(k)]
    for value in fields:
        if value in _BUDGET_TYPES:
            return value
    if status == 402:
        return fields[0][:64] if fields else "payment_required"
    if status in (403, 429):
        text = ""
        if isinstance(data, dict):
            detail = data.get("detail")
            if isinstance(detail, str):
                text = detail
            elif isinstance(error, dict) and isinstance(error.get("message"), str):
                text = error["message"]
        if "budget" in text.lower():
            return "budget"
    return None
