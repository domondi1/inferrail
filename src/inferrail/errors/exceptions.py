"""Inferrail's internal error hierarchy.

Provider-specific failures (HTTP status codes, client library exceptions,
timeouts) are normalized into these types at the provider boundary so that
routing, retry, telemetry, and the HTTP gateway never need to know about a
specific provider's error shapes.
"""

from __future__ import annotations


class InferrailError(Exception):
    """Base class for all errors raised by Inferrail."""

    #: Whether the inference execution loop may retry the request after
    #: this error. Overridden by subclasses that represent transient
    #: failures.
    retryable: bool = False


class ConfigurationError(InferrailError):
    """The Inferrail configuration is missing, invalid, or unusable.

    Raised at startup (config parsing, provider construction) or by
    `inferrail config check`. Never raised mid-request.
    """


class RoutingError(InferrailError):
    """A request could not be mapped to a configured route."""


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
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable


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
