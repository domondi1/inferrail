"""Stable, machine-readable error codes for every `InferrailError` subclass.

One code per exception type — enforced by
`tests/unit/test_error_codes.py`, mirroring the schema-level payload-field
guarantee tests elsewhere in this codebase. `ERRORS.md` is generated from
this table (`scripts/generate_errors_md.py`) — edit this file, not
`ERRORS.md` directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from inferrail.errors.exceptions import (
    AuthenticationError,
    ConfigurationError,
    GatewayAuthenticationError,
    InferrailError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    RoutingError,
    UnsupportedFeatureError,
)

DOCS_URL_BASE = "https://github.com/domondi1/inferrail/blob/main/ERRORS.md"


@dataclass(frozen=True)
class ErrorCode:
    code: str
    summary: str
    remediation: str


# Checked in order, most-specific subclass first — mirrors
# gateway/app.py's `_STATUS_BY_ERROR` table intentionally, since both
# answer "what kind of InferrailError is this" from the same hierarchy.
_CODES: list[tuple[type[InferrailError], ErrorCode]] = [
    (
        GatewayAuthenticationError,
        ErrorCode(
            code="INFERRAIL_E001",
            summary="Missing or invalid Inferrail gateway credentials.",
            remediation="Set the 'Authorization: Bearer <token>' header to match "
            "INFERRAIL_GATEWAY_TOKEN.",
        ),
    ),
    (
        AuthenticationError,
        ErrorCode(
            code="INFERRAIL_E002",
            summary="The upstream provider rejected Inferrail's own credentials.",
            remediation="Check the API key referenced by the provider's api_key_env "
            "in inferrail.yaml.",
        ),
    ),
    (
        RateLimitError,
        ErrorCode(
            code="INFERRAIL_E003",
            summary="The upstream provider is rate-limiting requests.",
            remediation="Safe to retry with backoff; Inferrail already retries this "
            "automatically up to the route's max_retries.",
        ),
    ),
    (
        ProviderTimeoutError,
        ErrorCode(
            code="INFERRAIL_E004",
            summary="The request to the upstream provider timed out.",
            remediation="Safe to retry; consider raising the route's timeout_seconds "
            "if this recurs.",
        ),
    ),
    (
        InvalidRequestError,
        ErrorCode(
            code="INFERRAIL_E005",
            summary="The upstream provider rejected the request as malformed.",
            remediation="Not retryable as-is; check the request body against the "
            "provider's own API docs.",
        ),
    ),
    (
        UnsupportedFeatureError,
        ErrorCode(
            code="INFERRAIL_E006",
            summary="The request used a feature Inferrail doesn't support yet.",
            remediation="Remove the unsupported field (e.g. stream, n != 1) — see "
            "docs/PRODUCT.md's current scope.",
        ),
    ),
    (
        RoutingError,
        ErrorCode(
            code="INFERRAIL_E007",
            summary="The request's model field didn't match a configured route.",
            remediation="Use one of the route names under inferrail.yaml's routes: "
            "section, not a provider model id.",
        ),
    ),
    (
        ConfigurationError,
        ErrorCode(
            code="INFERRAIL_E008",
            summary="Inferrail's own configuration is missing, invalid, or unusable.",
            remediation="Run 'inferrail config check' for the specific validation error.",
        ),
    ),
    (
        ProviderError,
        ErrorCode(
            code="INFERRAIL_E009",
            summary="The upstream provider request failed for an unrecognized reason.",
            remediation="Check provider status; Inferrail retries automatically if the "
            "failure looks transient (5xx).",
        ),
    ),
]

UNKNOWN_CODE = ErrorCode(
    code="INFERRAIL_E000",
    summary="An unrecognized internal error occurred.",
    remediation="This is a bug — please open an issue with the request id from the response.",
)


def code_for(exc: InferrailError) -> ErrorCode:
    for exc_type, error_code in _CODES:
        if isinstance(exc, exc_type):
            return error_code
    return UNKNOWN_CODE  # pragma: no cover - unreachable given today's hierarchy


def docs_url_for(code: str) -> str:
    return f"{DOCS_URL_BASE}#{code.lower()}"


def all_codes() -> list[tuple[type[InferrailError], ErrorCode]]:
    """Every registered (exception type, code) pair, for ERRORS.md generation and tests."""
    return list(_CODES)
