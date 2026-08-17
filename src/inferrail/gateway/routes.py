from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, Request

from inferrail.errors import GatewayAuthenticationError
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
    "shape Inferrail currently supports (see docs/PRODUCT.md): no `stream: true`, no "
    "`n != 1`, no tool calls, single string message content. `model` selects a named "
    "route from `inferrail.yaml`, not a provider model id directly (docs/adr/0002). "
    "Optional `X-Inferrail-Attribute-<Name>` headers attach business attribution "
    "(e.g. `X-Inferrail-Attribute-Customer: acme`), persisted on the resulting "
    "payload-free receipt and never forwarded upstream. The response is OpenAI-shaped "
    "plus a non-standard `inferrail` metadata block; standard OpenAI clients ignore it.",
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
) -> ChatCompletionResponse:
    engine: InferenceEngine = request.app.state.engine
    attributes = extract_attributes(request.headers)
    return await engine.execute(payload, attributes=attributes)
