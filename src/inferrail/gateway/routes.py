from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from inferrail.errors import GatewayAuthenticationError
from inferrail.gateway.anthropic_execution import AnthropicInferenceEngine
from inferrail.gateway.anthropic_schemas import MessagesRequest, MessagesResponse
from inferrail.gateway.attribution import extract_attributes
from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.schemas import ChatCompletionRequest, ChatCompletionResponse

router = APIRouter()


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
    "on the resulting payload-free receipt and never forwarded upstream. The non-streaming "
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
    attributes = extract_attributes(request.headers)
    if payload.stream:
        body = await engine.prepare_stream(payload, attributes=attributes)
        return StreamingResponse(body, media_type="text/event-stream")
    return await engine.execute(payload, attributes=attributes)


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
) -> MessagesResponse | StreamingResponse:
    engine: AnthropicInferenceEngine = request.app.state.anthropic_engine
    attributes = extract_attributes(request.headers)
    if payload.stream:
        body = await engine.prepare_stream(payload, attributes=attributes)
        return StreamingResponse(body, media_type="text/event-stream")
    return await engine.execute(payload, attributes=attributes)
