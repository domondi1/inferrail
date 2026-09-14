"""Inferrail's internal error hierarchy.

Provider-specific failures (HTTP status codes, client library exceptions,
timeouts) are normalized into these types at the provider boundary so that
routing, retry, telemetry, and the HTTP gateway never need to know about a
specific provider's error shapes.
"""

from __future__ import annotations

from decimal import Decimal


class InferrailError(Exception):
    """Base class for all errors raised by Inferrail."""

    #: Whether the inference execution loop may retry the request after
    #: this error. Overridden by subclasses that represent transient
    #: failures.
    retryable: bool = False

    @property
    def safe_summary(self) -> str:
        """A telemetry-safe summary of this error.

        Used for `InferenceEvent.error_message` instead of `str(self)`.
        Safe by default here because Inferrail's own locally-constructed
        error messages never embed request/response payload content — see
        `ProviderError`, which overrides this for errors built from
        upstream, provider-controlled text.
        """
        return str(self)


class ConfigurationError(InferrailError):
    """The Inferrail configuration is missing, invalid, or unusable.

    Raised at startup (config parsing, provider construction) or by
    `inferrail config check`. Never raised mid-request.
    """


class RoutingError(InferrailError):
    """A request could not be mapped to a configured route."""


class GatewayAuthenticationError(InferrailError):
    """The caller did not present a valid Inferrail gateway bearer token.

    Distinct from :class:`AuthenticationError`, which represents the
    *provider* rejecting Inferrail's own credentials. This one represents
    Inferrail rejecting the *client's* request, and is only raised when
    ``INFERRAIL_GATEWAY_TOKEN`` is set (see docs/PRODUCT.md).
    """


class UnsupportedFeatureError(InferrailError):
    """The request asked for something Inferrail does not yet support."""


class ProviderError(InferrailError):
    """A provider request failed. Base class for normalized provider errors."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
        safe_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable
        #: Optional telemetry-safe override of `safe_summary`, for callers
        #: that have verified `message` contains only categorical upstream
        #: fields (status code, error type/code) safe to persist. NEVER
        #: used as a signal to fall back to `message` itself — see
        #: `safe_summary` below, which is fail-closed by construction: if
        #: this is omitted, the default is derived only from `provider`/
        #: `status_code`, never from `message`, so a caller that forgets to
        #: pass this cannot silently leak upstream free text into
        #: telemetry.
        self._safe_summary = safe_summary

    @property
    def safe_summary(self) -> str:
        if self._safe_summary is not None:
            return self._safe_summary
        # Fail closed: `message` may embed upstream, provider-controlled
        # free text (some providers echo fragments of the request back in
        # error text — e.g. a content-policy rejection quoting the flagged
        # input), so it must never be the fallback here. This default is
        # built only from `provider` and `status_code`, which come from
        # Inferrail's own config and the HTTP status line respectively —
        # structurally incapable of carrying upstream free text — so it's
        # safe even when a caller (including a future provider adapter)
        # never learns this parameter exists.
        parts = [f"provider '{self.provider}'" if self.provider else "provider", "error"]
        if self.status_code is not None:
            parts.append(f"(HTTP {self.status_code})")
        return " ".join(parts)


class AuthenticationError(ProviderError):
    """The provider rejected our credentials (HTTP 401/403)."""


class InvalidRequestError(ProviderError):
    """The provider rejected the request as malformed (HTTP 400/404/422)."""


class RateLimitError(ProviderError):
    """The provider is rate-limiting us (HTTP 429). Safe to retry."""

    retryable = True


class ProviderTimeoutError(ProviderError):
    """The request to the provider timed out. Safe to retry.

    Named ``ProviderTimeoutError`` (not ``TimeoutError``) to avoid shadowing
    the built-in exception of the same name.
    """

    retryable = True


class BudgetExceededError(InferrailError):
    """A "block"-mode budget would be exceeded by this request — raised
    before the provider is ever contacted (see
    ``inferrail.budgets.enforcement.BudgetEnforcer.check``). Every field
    here is structurally incapable of carrying prompt/response content
    (all of it is numbers, or config the operator themselves wrote), so
    unlike ``ProviderError`` no separate ``safe_summary`` override is
    needed — the default (``str(self)``) is already telemetry-safe.

    Never raised for a "warn"-mode budget — see
    ``docs/adr/0015-budget-enforcement.md``.
    """

    def __init__(
        self,
        *,
        budget_id: str,
        scope: str,
        scope_value: str | None,
        window: str,
        mode: str,
        limit_usd: Decimal,
        spent_so_far_usd: Decimal,
        estimated_request_usd: Decimal,
        projected_total_usd: Decimal,
    ) -> None:
        scoped = f"{scope}={scope_value}" if scope_value is not None else scope
        message = (
            f"budget '{budget_id}' ({scoped}, window={window}, mode={mode}) would be "
            f"exceeded: spent ${spent_so_far_usd} + estimated ${estimated_request_usd} "
            f"= ${projected_total_usd} > limit ${limit_usd}"
        )
        super().__init__(message)
        self.budget_id = budget_id
        self.scope = scope
        self.scope_value = scope_value
        self.window = window
        self.mode = mode
        self.limit_usd = limit_usd
        self.spent_so_far_usd = spent_so_far_usd
        self.estimated_request_usd = estimated_request_usd
        self.projected_total_usd = projected_total_usd
