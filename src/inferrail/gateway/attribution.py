"""Caller-supplied business attribution: the generic, non-vertical-specific
mechanism for attaching context (customer, workflow, tenant, feature,
environment, project, ...) to a request.

Deliberately transported as HTTP headers, not a JSON body field: the OpenAI
`/v1/chat/completions` request body stays byte-for-byte what an unmodified
OpenAI client sends (see docs/adr/0001), and the header value never enters
`NormalizedChatRequest` — the type `providers.openai.OpenAIProvider.complete`
consumes — so attribution data is structurally incapable of being forwarded
to the upstream provider; there is no stripping step to forget.

Attribute values ARE persisted, verbatim, in `InferenceReceipt.attributes`
(see receipts/schema.py). Do not put secrets or other sensitive data in
them. See docs/integrations.md's "Attribution" section.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from inferrail.errors import BudgetDeclarationError

_HEADER_PREFIX = "x-inferrail-attribute-"
BUDGET_HEADER = "x-inferrail-budget-usd"
# A sanity bound on a declared per-run ceiling, not a policy: operators set
# their own with budgets.per_work_max_usd (docs/adr/0022).
_MAX_DECLARABLE_USD = Decimal("1000000")


def extract_attributes(
    headers: Mapping[str, str], work_id_headers: Sequence[str] = ()
) -> dict[str, str]:
    """Collect `X-Inferrail-Attribute-<Name>` headers into `{name: value}`.

    Header names are case-insensitive (per HTTP) and matched
    case-insensitively here; the resulting key is the lowercased,
    hyphen-to-underscore-normalized suffix, e.g.
    `X-Inferrail-Attribute-Workflow-Type` -> `workflow_type`. A header with
    an empty name suffix or an empty value is dropped rather than stored,
    since neither carries any attributable meaning.

    `work_id_headers` (config `work_id_headers`) names other headers to take
    the `work_id` from when no `X-Inferrail-Attribute-Work-Id` was sent,
    first non-empty match wins. An explicit work id header always takes
    precedence.
    """
    attributes: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        lowered = raw_name.lower()
        if not lowered.startswith(_HEADER_PREFIX):
            continue
        key = lowered[len(_HEADER_PREFIX) :].replace("-", "_")
        if not key or not raw_value:
            continue
        attributes[key] = raw_value
    if "work_id" not in attributes and work_id_headers:
        lowered_headers = {name.lower(): value for name, value in headers.items()}
        for name in work_id_headers:
            value = lowered_headers.get(name.lower())
            if value:
                attributes["work_id"] = value
                break
    return attributes


def extract_declared_budget(headers: Mapping[str, str]) -> Decimal | None:
    """Parse `X-Inferrail-Budget-Usd`, a run's declared dollar ceiling
    (docs/adr/0022-per-run-budget-declaration.md). `None` when absent.
    Anything that isn't a finite, positive decimal (up to a sanity bound)
    is refused with `BudgetDeclarationError` before any provider call,
    never ignored. Unlike attribute headers, the value is not stored as an
    attribute; it becomes the run's budget, and it is never forwarded
    upstream."""
    raw = None
    for name, value in headers.items():
        if name.lower() == BUDGET_HEADER:
            raw = value
            break
    if raw is None:
        return None
    try:
        amount = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        amount = None
    if amount is None or not amount.is_finite() or amount <= 0 or amount > _MAX_DECLARABLE_USD:
        raise BudgetDeclarationError(
            reason="invalid",
            message="X-Inferrail-Budget-Usd must be a positive decimal amount in USD, "
            f"at most {_MAX_DECLARABLE_USD} (e.g. 0.50)",
        )
    return amount
