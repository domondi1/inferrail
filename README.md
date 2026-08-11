# Inferrail

Inferrail is an open inference control plane: an OpenAI-compatible gateway
that sits in front of your LLM provider(s), so routing, retries, and
per-request telemetry live in configuration instead of scattered through
application code.

**Status: early, pre-release (v0.1.0).** This is one narrow vertical slice,
not a finished product — see [what works today](#what-works-today) below
and [docs/PRODUCT.md](docs/PRODUCT.md) for exact scope and non-goals.

## What works today

- `POST /v1/chat/completions` — OpenAI-compatible request/response shape
  (single string message content; no streaming, no tool calls, no `n != 1`
  — see [docs/PRODUCT.md](docs/PRODUCT.md) for the exact list)
- `GET /health`
- One provider adapter, generic over any OpenAI-compatible HTTP endpoint
  (OpenAI itself, or a compatible self-hosted server)
- Static, explicit routing: your request's `model` field selects a named
  route from `inferrail.yaml`
- Retries with backoff for transient provider errors (timeouts, rate
  limits, 5xx), configurable per route
- A structured telemetry record for every request — latency, token counts,
  retry count, success/failure — logged locally (console or a JSONL file).
  **No prompts or responses are persisted by default, and nothing is ever
  sent to any Inferrail-operated service.**

**Not yet supported:** streaming, multi-provider intelligent routing, cost
estimates, any provider that isn't OpenAI-compatible. Full list in
[docs/PRODUCT.md](docs/PRODUCT.md).

## Install

Requires Python 3.11+.

```bash
git clone <this-repo>
cd inferrail
pip install -e .
```

## Configure

```bash
cp inferrail.example.yaml inferrail.yaml
cp .env.example .env
```

Edit `.env` and set a real `OPENAI_API_KEY`. `inferrail.yaml` only ever
references the *name* of the environment variable holding a secret, never
the secret itself — see that file for the full shape (providers, routes,
telemetry, server).

Validate your setup without starting a server:

```bash
inferrail config check
```

## Run

```bash
inferrail serve
```

By default this starts the gateway on `http://127.0.0.1:8000`.

## Send your first request

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Say hello in five words."}]
  }'
```

`"model": "default"` refers to the `default` route in `inferrail.yaml`, not
a provider's model id directly — Inferrail resolves it to whatever
provider/model that route points at. See
[examples/basic_chat_request.py](examples/basic_chat_request.py) for a
minimal Python client, and
[docs/adr/0002-static-deterministic-routing.md](docs/adr/0002-static-deterministic-routing.md)
for why routing works this way.

The response includes the standard OpenAI `choices`/`usage` fields plus a
non-standard `inferrail` field with routing/latency metadata:

```json
{
  "choices": [{"message": {"role": "assistant", "content": "..."}}],
  "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
  "inferrail": {
    "request_id": "req_...",
    "route": "default",
    "provider": "openai",
    "total_latency_ms": 842.1,
    "retry_count": 0
  }
}
```

Every request also produces a local `InferenceEvent` on the telemetry sink
configured in `inferrail.yaml` (`console` by default) — this is where
you'd look to see latency, retries, and failures across requests.

## Run the tests

```bash
pip install -e ".[dev]"
pytest
```

All automated tests run against a mocked provider transport — no API key
or network access required, and no cost is incurred. One additional
integration test in `tests/integration/` makes a real call to OpenAI and
is automatically skipped unless `OPENAI_API_KEY` is set.

```bash
ruff check .
mypy src
```

## Security

Inferrail is a **local development gateway by default**: it binds to
`127.0.0.1` and, unless you configure otherwise, accepts requests from
anything that can reach that port with no authentication of its own.

**This matters because Inferrail holds your upstream provider credentials.**
If you expose the gateway beyond your own machine (bind it to `0.0.0.0`,
port-forward it, put it behind a reverse proxy, run it in a shared
container network, etc.) without an authentication layer, anyone who can
reach it can make inference calls billed to your configured provider
account.

To require callers to authenticate, set `INFERRAIL_GATEWAY_TOKEN` (see
`.env.example`) to a long random value before starting the gateway.
Requests to `/v1/chat/completions` must then include
`Authorization: Bearer <token>`, or Inferrail rejects them with 401 before
touching any provider. `/health` never requires it. This is a single
shared-secret check, not a user/auth system — sufficient for "don't let
strangers spend my API budget," not for multi-tenant access control.

Also see [privacy](#privacy) below for what does and doesn't leave the
machine.

## Privacy

By default, and structurally (not just by configuration), Inferrail never
sends anything to an Inferrail-operated service — there isn't one yet.
Local telemetry (`InferenceEvent`) is schema-limited to operational
metadata: request id, route, provider, model, status, latency, token
counts, retry count. There is no field for prompt or response text, so
enabling `jsonl` telemetry cannot leak message content even by accident.
See [docs/adr/0003-no-payload-persistence-by-default.md](docs/adr/0003-no-payload-persistence-by-default.md).

What Inferrail does send off-machine: your configured provider (e.g.
OpenAI) still receives the actual prompt, exactly as it would if you
called it directly — Inferrail is a pass-through gateway to that provider,
not a privacy boundary against it.

## Why "Inferrail"

The rails on which AI inference runs. See
[docs/PRODUCT.md](docs/PRODUCT.md) for the full product thesis and
long-term direction, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
how the pieces fit together and the boundary between this open-source data
plane and any future hosted control plane.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
