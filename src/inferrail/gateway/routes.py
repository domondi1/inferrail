from __future__ import annotations

from fastapi import APIRouter, Request

from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.schemas import ChatCompletionRequest, ChatCompletionResponse

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    payload: ChatCompletionRequest, request: Request
) -> ChatCompletionResponse:
    engine: InferenceEngine = request.app.state.engine
    return await engine.execute(payload)
