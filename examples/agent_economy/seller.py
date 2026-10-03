"""A stock x402 seller: paid search and a paid OpenAI-shaped chat endpoint.

There is no Inferrail code here on purpose. Any x402 seller accepts any
x402 payer, so a budget-bounded buyer needs nothing from the seller. The
work is deterministic so the demo and its tests are reproducible: search
ranks a small fixed corpus, and "chat" returns an extractive summary.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import register_exact_evm_server
from x402.server import x402ResourceServer

NETWORK = "eip155:84532"
SEARCH_PRICE_USD = "0.002"
CHAT_PRICE_USD = "0.003"

CORPUS = [
    ("x402", "x402 is an HTTP payment protocol: a 402 response carries payment requirements."),
    ("eip-3009", "EIP-3009 lets a token holder sign a single-use, time-bound transfer."),
    ("budgets", "A run budget bounds what one unit of agent work may spend in total."),
    ("delegation", "A parent can delegate part of its authority to a sub-agent."),
    ("reconciliation", "Reconciliation checks recorded spend against what actually settled."),
]


def search(query: str) -> list[dict[str, Any]]:
    words = {w for w in query.lower().split() if w}
    scored = []
    for doc_id, text in CORPUS:
        score = sum(1 for w in words if w in text.lower() or w == doc_id)
        if score:
            scored.append({"id": doc_id, "text": text, "score": score})
    return sorted(scored, key=lambda r: (-r["score"], r["id"]))


def summarize(text: str, max_words: int = 12) -> str:
    return " ".join(text.split()[:max_words])


def create_seller_app(facilitator: Any, pay_to: str, base_url: str) -> FastAPI:
    server = x402ResourceServer(facilitator)
    register_exact_evm_server(server, networks=NETWORK)

    def paid(price: str, path: str, description: str) -> RouteConfig:
        return RouteConfig(
            accepts=PaymentOption(
                scheme="exact", pay_to=pay_to, price=f"${price}", network=NETWORK
            ),
            resource=f"{base_url}{path}",
            description=description,
        )

    routes = {
        "GET /search": paid(SEARCH_PRICE_USD, "/search", "Ranked search over a small corpus"),
        "POST /v1/chat/completions": paid(
            CHAT_PRICE_USD, "/v1/chat/completions", "OpenAI-shaped chat completion"
        ),
    }
    middleware = payment_middleware(routes, server)
    app = FastAPI()

    @app.middleware("http")
    async def x402(request: Request, call_next: Any) -> Any:
        return await middleware(request, call_next)

    @app.get("/search")
    async def search_route(q: str) -> dict[str, Any]:
        return {"query": q, "results": search(q)}

    @app.post("/v1/chat/completions")
    async def chat_route(body: dict[str, Any]) -> dict[str, Any]:
        prompt = " ".join(str(m.get("content", "")) for m in body.get("messages", []))
        return {
            "object": "chat.completion",
            "model": body.get("model", "seller-summarizer"),
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": summarize(prompt)}}
            ],
        }

    return app
