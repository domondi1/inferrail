# Inferrail

Inferrail is an open inference control plane: an OpenAI-compatible gateway
that sits in front of your LLM provider(s), so routing, retries, and
per-request telemetry live in configuration instead of scattered through
application code — and turns every execution into a payload-free economic
receipt, so you can see what each customer or workflow is costing you
without storing their prompts.

**Status: early, pre-release (v0.1.0).** This is one narrow vertical slice,
not a finished product — see [what works today](#what-works-today) below
and [docs/PRODUCT.md](docs/PRODUCT.md) for exact scope and non-goals.

## Quickstart

Requires Python 3.11+.

```bash
git clone https://github.com/domondi1/inferrail.git
cd inferrail
pip install -e .
```

### Fastest — see the shape of it (no key, no network)

```bash
inferrail demo
```

Runs six canned requests through Inferrail's **real** `InferenceEngine` →
`InferenceReceipt` → `inferrail report` pipeline, using a fake in-memory
provider instead of a network call — no key, no signup, no cost, done in
under a second. Every price and response it uses is clearly labeled
**DEMO** data, not real provider billing.

### Real — one request, a real cost

```bash
export OPENAI_API_KEY=sk-...
inferrail try "Reply with one word: ready" --customer acme
```

No `inferrail.yaml`, no server, no curl, no HTTP headers. `inferrail try`
sends one real request through the exact same `InferenceEngine`
`inferrail serve` uses (OpenAI, `gpt-4o-mini` by default) and prints the
response plus its economic receipt — measured token usage, a `Decimal`
cost from verified pricing, your attribution, the receipt id, and an
explicit statement of what was and wasn't persisted:

```
ready

Receipt ir_670b20135cfe4bcb8f6f
  Provider          openai
  Model             gpt-4o-mini
  Input tokens      12
  Output tokens     1
  Cost              $0.000002
  Customer          acme
  Prompt stored     no
  Response stored   no

Saved to ./inferrail-receipts.jsonl

Next:
  inferrail report --by customer
```

Attach more business context generically with repeatable `-a KEY=VALUE`
(`--customer`/`--workflow` are just shorthand for `-a customer=...`/
`-a workflow=...`):

```bash
inferrail try "Summarize this ticket" -a customer=acme -a environment=prod
```

No `OPENAI_API_KEY`? `inferrail try` tells you to run `inferrail demo`
instead, rather than failing with a stack trace.

### Application — run it as a gateway

```bash
inferrail serve --quickstart
```

Same defaults as `inferrail try` (OpenAI, `gpt-4o-mini`, receipts at
`./inferrail-receipts.jsonl`), no `inferrail.yaml` required — every
`/v1/chat/completions` request now produces the same kind of receipt
`inferrail try` showed you above. `inferrail serve --quickstart` never
writes a config file; it's in-memory defaults only.

For a real deployment with explicit, checked-in configuration instead:

```bash
cp inferrail.example.yaml inferrail.yaml
cp .env.example .env            # then edit .env with a real OPENAI_API_KEY
inferrail config check          # validate without starting a server
inferrail serve
```

See [Send requests through the gateway](#send-requests-through-the-gateway)
for `curl`/attribution-header usage, and [Configure](#configure) for the
full `inferrail.yaml` shape (multiple routes, retries, telemetry, pricing
overrides).

### Report — what is this costing me

```bash
inferrail report --by customer
```

```
CUSTOMER        REQUESTS  INPUT TOKENS  OUTPUT TOKENS  COST (USD)  UNKNOWN COST
acme            5         3368          764            $0.000964
globex          2         1684          382            $0.000482
(unattributed)  1         842           191            $0.000241
--------------  --------  ------------  -------------  ----------  ------------
TOTAL           8         5894          1337           $0.001687
```

`--by` also accepts `provider`, `model`, `route`, or any other attribute
name you've sent. Works immediately after `inferrail try`/`inferrail serve
--quickstart` (no `--config` needed — it reads the same default
`./inferrail-receipts.jsonl`). See [Cost and attribution](#cost-and-attribution)
for where pricing comes from and what `UNKNOWN COST` means.

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
- A **payload-free economic receipt** for every request: provider, model,
  measured token usage, a `Decimal` cost computed from verified pricing
  (with its source and date recorded), and whatever business context you
  attach — with no field capable of holding prompt or response text. See
  [Cost and attribution](#cost-and-attribution) below.
- `inferrail report --by <dimension>` — turns those receipts into an
  immediate, local answer to "what is this customer/workflow costing me".
- A CLI: `inferrail demo` (offline walkthrough), `inferrail try` (one real
  request, no config file), `inferrail serve` (the gateway, optionally
  `--quickstart`), `inferrail config check`, `inferrail report`.

**Not yet supported:** streaming, multi-provider intelligent routing, cost
estimates for models outside the built-in catalog or an explicit
`pricing:` override, budgets/spend limits, any provider that isn't
OpenAI-compatible. Full list in [docs/PRODUCT.md](docs/PRODUCT.md).

## Send requests through the gateway

With `inferrail serve` (or `inferrail serve --quickstart`) running:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Say hello in five words."}]
  }'
```

`"model": "default"` refers to the `default` route in `inferrail.yaml` (or
the quickstart config), not a provider's model id directly — Inferrail
resolves it to whatever provider/model that route points at. See
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

Attach business context with `X-Inferrail-Attribute-<Name>` headers — the
HTTP-header equivalent of `inferrail try`'s `--customer`/`--workflow`/`-a`
flags:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Inferrail-Attribute-Customer: acme" \
  -H "X-Inferrail-Attribute-Workflow: contract-review" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Say hello in five words."}]
  }'
```

Any `X-Inferrail-Attribute-<Name>` header becomes a key in the receipt's
`attributes` — there's no fixed set of dimensions (`customer` and
`workflow` above are just examples). **Attribute values are persisted
verbatim in the receipt** — don't put secrets or other sensitive data in
them. They are never forwarded to the upstream provider.

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

Then:

```bash
inferrail serve
```

By default this starts the gateway on `http://127.0.0.1:8000`. (No
`inferrail.yaml` yet? `inferrail serve --quickstart` skips this whole
section — see [Application](#application--run-it-as-a-gateway) above.)

## Cost and attribution

Every request also produces a local **`InferenceReceipt`** — appended by
default to `./inferrail-receipts.jsonl` (`receipts.path` in
`inferrail.yaml`, or the quickstart default of the same name for
`inferrail try`/`inferrail serve --quickstart`).

The resulting receipt (one JSON line in `inferrail-receipts.jsonl`):

```json
{
  "receipt_id": "ir_1e6c916bac8940ca8a85",
  "request_id": "req_62f023c643fd4f4285d1",
  "timestamp": "2026-08-16T06:12:23.486756Z",
  "route": "default",
  "provider": "openai",
  "model": "gpt-4o-mini",
  "status": "success",
  "prompt_tokens": 842,
  "completion_tokens": 191,
  "pricing": {
    "input_usd_per_million": "0.15",
    "output_usd_per_million": "0.60",
    "source": "https://developers.openai.com/api/docs/pricing",
    "verified_date": "2026-08-16"
  },
  "estimated_cost_usd": "0.000241",
  "attributes": { "customer": "acme", "workflow": "contract-review" },
  "total_latency_ms": 15.96,
  "retry_count": 0
}
```

**Where pricing comes from:** a small built-in catalog, independently
verified against OpenAI's own published pricing (`gpt-4o-mini`, `gpt-4o`
today — see `src/inferrail/pricing/builtin.py`), applied only when a
provider is configured as `type: openai` with no custom `base_url` (i.e.
verifiably OpenAI's real API — never guessed onto a same-shaped
`openai_compatible` endpoint that could be running something else). For
anything else, add a `pricing:` entry to `inferrail.yaml` — see
`inferrail.example.yaml` for the shape. An unresolvable price leaves
`pricing`/`estimated_cost_usd` as `null`, never a fabricated `$0` — `inferrail
report`'s `UNKNOWN COST` column counts these separately rather than folding
them into the cost total. All cost arithmetic uses `Decimal`, never
`float`. See
[docs/adr/0005-privacy-preserving-economic-receipts.md](docs/adr/0005-privacy-preserving-economic-receipts.md)
for the full reasoning.

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

The same guarantee applies to economic receipts (`InferenceReceipt`,
enabled by default — see [Cost and attribution](#cost-and-attribution)):
provider, model, token counts, cost, pricing provenance, business
attribution, latency, status. No field can hold prompt or response text.
The one deliberate exception is `attributes` — caller-supplied business
context you explicitly attached (via `inferrail try`'s flags or
`X-Inferrail-Attribute-*` headers) is persisted verbatim, since it's
metadata you declared, not content extracted from the conversation. See
[docs/adr/0005-privacy-preserving-economic-receipts.md](docs/adr/0005-privacy-preserving-economic-receipts.md).

What Inferrail does send off-machine: your configured provider (e.g.
OpenAI) still receives the actual prompt, exactly as it would if you
called it directly — Inferrail is a pass-through gateway to that provider,
not a privacy boundary against it.

### Verify it yourself

Don't take Inferrail's local no-payload-persistence claim on faith — check
it in under a minute. This checks only what Inferrail itself writes to
disk; your provider still receives the real prompt either way (see above).

In `inferrail.yaml`, set:

```yaml
telemetry:
  sink: jsonl
  path: inferrail-telemetry.jsonl
```

Restart `inferrail serve`, then:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "default", "messages": [{"role": "user", "content": "MARKER-1234-do-not-persist-me"}]}'

grep -c "MARKER-1234" inferrail-telemetry.jsonl   # 0, every time
cat inferrail-telemetry.jsonl                     # latency, tokens, status — no message content

grep -c "MARKER-1234" inferrail-receipts.jsonl    # 0, every time (receipts are on by default)
cat inferrail-receipts.jsonl                      # tokens, cost, pricing — no message content
```

The same check works with `inferrail try "MARKER-1234-do-not-persist-me"` —
no `inferrail.yaml` edit required, since `inferrail try` never persists
telemetry by default (`telemetry.sink: none` in its quickstart config) and
receipts are schema-limited the same way either path.

## Why "Inferrail"

The rails on which AI inference runs. See
[docs/PRODUCT.md](docs/PRODUCT.md) for the full product thesis and
long-term direction, and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for
how the pieces fit together and the boundary between this open-source data
plane and any future hosted control plane.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
