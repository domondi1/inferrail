"""Send one message through a locally running Inferrail's Anthropic-
compatible `/v1/messages` passthrough (see
docs/adr/0014-anthropic-messages-passthrough.md).

Usage:
    # inferrail.yaml needs an `anthropic:`-typed provider + a route for
    # it (see inferrail.example.yaml's commented example) and
    # ANTHROPIC_API_KEY set in the environment.
    inferrail serve   # in one terminal
    python examples/anthropic_messages_request.py   # in another

Uses plain httpx rather than the `anthropic` package so this example has
no dependency beyond what Inferrail itself already requires.
"""

from __future__ import annotations

import httpx

INFERRAIL_URL = "http://127.0.0.1:8000/v1/messages"


def main() -> None:
    response = httpx.post(
        INFERRAIL_URL,
        json={
            "model": "claude",  # an Inferrail route name, see inferrail.yaml
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Say hello in exactly five words."}],
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()

    for block in body["content"]:
        if block.get("type") == "text":
            print(block["text"])
    print("---")
    print("inferrail metadata:", body["inferrail"])
    print("usage:", body["usage"])


if __name__ == "__main__":
    main()
