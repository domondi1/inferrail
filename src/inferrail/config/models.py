"""Typed schema for ``inferrail.yaml``.

Config is data, not code: parsing this file should never require network
access or provider SDKs. Building runnable provider instances from a parsed
:class:`InferrailConfig` (which does require reading secrets out of the
environment) happens separately in ``inferrail.providers.registry``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ProviderType = Literal["openai", "openai_compatible"]

_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class ProviderConfig(BaseModel):
    """A named upstream inference provider.

    ``type: openai`` and ``type: openai_compatible`` currently resolve to
    the same adapter (:class:`inferrail.providers.openai.OpenAIProvider`);
    the distinction exists so config reads clearly and so a future
    non-OpenAI-shaped provider can be added as a new ``type`` without
    touching this schema's shape.
    """

    model_config = {"extra": "forbid"}

    type: ProviderType
    api_key_env: str
    base_url: str | None = None

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        if self.type == "openai":
            return _DEFAULT_OPENAI_BASE_URL
        raise ValueError(
            f"provider type '{self.type}' requires an explicit base_url"
        )


class RouteConfig(BaseModel):
    """A named route: what a client selects via the request's ``model`` field."""

    model_config = {"extra": "forbid"}

    provider: str
    model: str
    max_retries: int = Field(default=0, ge=0, le=5)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)


class TelemetryConfig(BaseModel):
    model_config = {"extra": "forbid"}

    sink: Literal["console", "jsonl", "none"] = "console"
    path: str | None = None

    @model_validator(mode="after")
    def _require_path_for_jsonl(self) -> TelemetryConfig:
        if self.sink == "jsonl" and not self.path:
            raise ValueError("telemetry.path is required when telemetry.sink is 'jsonl'")
        return self


class ServerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, le=65535)


class InferrailConfig(BaseModel):
    model_config = {"extra": "forbid"}

    providers: dict[str, ProviderConfig]
    routes: dict[str, RouteConfig]
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @model_validator(mode="after")
    def _routes_reference_known_providers(self) -> InferrailConfig:
        if not self.providers:
            raise ValueError("at least one provider must be configured under 'providers'")
        if not self.routes:
            raise ValueError("at least one route must be configured under 'routes'")
        for route_name, route in self.routes.items():
            if route.provider not in self.providers:
                raise ValueError(
                    f"route '{route_name}' references unknown provider '{route.provider}'; "
                    f"known providers: {sorted(self.providers)}"
                )
        return self
