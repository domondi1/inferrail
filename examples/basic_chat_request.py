"""Send one chat completion request through a locally running Inferrail.

Usage:
    inferrail serve   # in one terminal
    python examples/basic_chat_request.py   # in another

Uses plain httpx rather than the `openai` package so this example has no
dependency beyond what Inferrail itself already requires.
"""

from __future__ import annotations

import httpx

INFERRAIL_URL = "http://127.0.0.1:8000/v1/chat/completions"


def main() -> None:
    response = httpx.post(
        INFERRAIL_URL,
        json={
            "model": "default",  # an Inferrail route name, see inferrail.yaml
            "messages": [{"role": "user", "content": "Say hello in exactly five words."}],
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()

    print(body["choices"][0]["message"]["content"])
    print("---")
    print("inferrail metadata:", body["inferrail"])
    print("usage:", body["usage"])


if __name__ == "__main__":
    main()
