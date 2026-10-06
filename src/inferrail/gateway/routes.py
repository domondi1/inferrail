from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from inferrail.errors import GatewayAuthenticationError, InferrailError
from inferrail.gateway.anthropic_execution import AnthropicInferenceEngine
from inferrail.gateway.anthropic_schemas import (
    MessagesRequest,
    MessagesResponse,
    absent_cache_usage_fields,
)
from inferrail.gateway.attribution import extract_attributes, extract_declared_budget
from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.schemas import ChatCompletionRequest, ChatCompletionResponse

router = APIRouter()
logger = logging.getLogger("inferrail.gateway")


async def _require_gateway_token(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    """Enforce ``INFERRAIL_GATEWAY_TOKEN`` if one is configured.

    A no-op when the token isn't set (the localhost-dev default) — see
    docs/PRODUCT.md. Uses a constant-time comparison since this is a
    bearer-secret check.
    """
    expected_token: str | None = request.app.state.gateway_token
    if expected_token is None:
        return
    provided = (authorization or "").removeprefix("Bearer ")
    if not secrets.compare_digest(provided, expected_token):
        raise GatewayAuthenticationError(
            "missing or invalid gateway credentials: set the 'Authorization: "
            "Bearer <token>' header to match INFERRAIL_GATEWAY_TOKEN"
        )


@router.get(
    "/health",
    operation_id="health",
    summary="Liveness check",
    description="Always returns 200 with {\"status\": \"ok\"} once the process is up. "
    "Does not verify provider connectivity or configuration validity — see "
    "`inferrail config check` for that.",
    responses={200: {"content": {"application/json": {"example": {"status": "ok"}}}}},
)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/v1/models",
    operation_id="listModels",
    summary="List models",
    description="OpenAI-compatible model list, so clients that populate a model picker "
    "from `GET /v1/models` work unchanged. Lists every route named in `inferrail.yaml` "
    "and, when `default_provider` is set (model ids pass through), the ids that "
    "provider's own `GET /models` returns, fetched live with the configured key and "
    "never cached. If that upstream list can't be fetched, only the routes are listed. "
    "Listing a model says nothing about whether Inferrail has a price for it; see "
    "`inferrail models`.",
    dependencies=[Depends(_require_gateway_token)],
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "object": "list",
                        "data": [
                            {"id": "gpt-4o-mini", "object": "model", "created": 0,
                             "owned_by": "openai"}
                        ],
                    }
                }
            }
        }
    },
)
async def list_models(request: Request) -> dict[str, object]:
    config = request.app.state.config
    owners: dict[str, str] = {name: route.provider for name, route in config.routes.items()}
    default_provider = config.default_provider
    provider = request.app.state.providers.get(default_provider) if default_provider else None
    if provider is not None and hasattr(provider, "list_models"):
        try:
            for model_id in await provider.list_models():
                owners.setdefault(model_id, default_provider)
        except InferrailError as exc:
            logger.warning("model list unavailable, listing routes only: %s", exc)
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": 0, "owned_by": owner}
            for model_id, owner in sorted(owners.items())
        ],
    }


@router.post(
    "/v1/chat/completions",
    operation_id="createChatCompletion",
    summary="Create a chat completion",
    description="OpenAI-compatible `/v1/chat/completions`, for the subset of the request "
    "shape Inferrail currently supports (see docs/PRODUCT.md): `stream: true` (real SSE "
    "passthrough, not buffered), tool calling (`tools`/`tool_choice`/`parallel_tool_calls`, "
    "including parallel and streamed tool calls), but not `n != 1` or multi-part/image "
    "message content. `model` selects a named route from `inferrail.yaml`, not a provider "
    "model id directly (docs/adr/0002). Optional `X-Inferrail-Attribute-<Name>` headers "
    "attach business attribution (e.g. `X-Inferrail-Attribute-Customer: acme`), persisted "
    "on the resulting payload-free receipt and never forwarded upstream. With "
    "`X-Inferrail-Attribute-Work-Id`, `X-Inferrail-Budget-Usd` declares that run's dollar "
    "ceiling (docs/adr/0022), also never forwarded. The non-streaming "
    "response is OpenAI-shaped plus a non-standard `inferrail` metadata block; standard "
    "OpenAI clients ignore it. A streaming response is a plain upstream-shaped SSE stream "
    "with no such metadata injected into it, to preserve exact protocol fidelity.",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(_require_gateway_token)],
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "example": {
                        "model": "default",
                        "messages": [{"role": "user", "content": "Say hello in five words."}],
                    }
                }
            }
        }
    },
)
async def chat_completions(
    payload: ChatCompletionRequest, request: Request
) -> ChatCompletionResponse | StreamingResponse:
    engine: InferenceEngine = request.app.state.engine
    attributes = extract_attributes(
        request.headers, request.app.state.config.work_id_headers
    )
    declared = extract_declared_budget(request.headers)
    if payload.stream:
        body = await engine.prepare_stream(
            payload, attributes=attributes, declared_budget_usd=declared
        )
        return StreamingResponse(body, media_type="text/event-stream")
    return await engine.execute(payload, attributes=attributes, declared_budget_usd=declared)


@router.post(
    "/v1/messages",
    operation_id="createMessage",
    summary="Create a message (Anthropic-compatible)",
    description="Anthropic-compatible `/v1/messages` — a genuine wire-native passthrough "
    "(see docs/adr/0014), not a translation of the OpenAI-shaped `/v1/chat/completions` "
    "contract: `system`, message content blocks, `tools`/`tool_choice`, and `stream: true` "
    "(real SSE passthrough) are all forwarded as sent/received, byte-for-byte while "
    "streaming. `model` selects a named route from `inferrail.yaml`, not a provider model "
    "id directly (docs/adr/0002), same convention as `/v1/chat/completions`. Optional "
    "`X-Inferrail-Attribute-<Name>` headers attach business attribution, persisted on the "
    "resulting payload-free receipt and never forwarded upstream. The non-streaming "
    "response is Anthropic-shaped plus a non-standard `inferrail` metadata block; standard "
    "Anthropic clients ignore it. A streaming response has no such metadata injected, to "
    "preserve exact protocol fidelity.",
    response_model=MessagesResponse,
    dependencies=[Depends(_require_gateway_token)],
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "example": {
                        "model": "claude",
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": "Say hello in five words."}],
                    }
                }
            }
        }
    },
)
async def messages(
    payload: MessagesRequest, request: Request
) -> MessagesResponse | StreamingResponse | JSONResponse:
    engine: AnthropicInferenceEngine = request.app.state.anthropic_engine
    attributes = extract_attributes(
        request.headers, request.app.state.config.work_id_headers
    )
    declared = extract_declared_budget(request.headers)
    anthropic_beta = request.headers.get("anthropic-beta")
    beta_query = request.query_params.get("beta") == "true"
    if payload.stream:
        body = await engine.prepare_stream(
            payload, attributes=attributes, declared_budget_usd=declared,
            anthropic_beta=anthropic_beta, beta_query=beta_query,
        )
        return StreamingResponse(body, media_type="text/event-stream")
    result = await engine.execute(
        payload, attributes=attributes, declared_budget_usd=declared,
        anthropic_beta=anthropic_beta, beta_query=beta_query,
    )
    # Serialized here rather than by `response_model` so prompt-cache usage
    # fields the provider didn't report are left out, not sent as null: an
    # uncached response stays exactly what it was. The documented schema
    # (`response_model` above) is unchanged.
    return JSONResponse(
        result.model_dump(
            mode="json", exclude={"usage": absent_cache_usage_fields(result.usage)}
        )
    )
