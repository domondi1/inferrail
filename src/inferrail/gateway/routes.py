from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, Request

from inferrail.errors import GatewayAuthenticationError
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


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse,
    dependencies=[Depends(_require_gateway_token)],
)
async def chat_completions(
    payload: ChatCompletionRequest, request: Request
) -> ChatCompletionResponse:
    engine: InferenceEngine = request.app.state.engine
    return await engine.execute(payload)
