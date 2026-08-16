# 0005. A separate, privacy-preserving `InferenceReceipt` for economic data

## Status

Accepted

## Context

v0.1 gave Inferrail a real telemetry record (`InferenceEvent`) for every
request, but its `estimated_cost_usd` field was always `null` — there was
no verified pricing table yet (see docs/PRODUCT.md's non-goals at the
time). Turning "gateway with good foundations" into something a stranger
would clone requires the first real economic primitive: knowing what an
inference execution cost, and to which business context (customer,
workflow, ...) that cost belongs — without persisting the prompt or
response that produced it.

Three questions had to be answered before writing code:

1. Does cost belong on `InferenceEvent`, or as a new, separate record?
2. Where does verified pricing come from, and how is "unknown" kept
   distinct from "free"?
3. How does caller-supplied business context reach the gateway without
   compromising OpenAI wire compatibility or accidentally forwarding
   Inferrail-only metadata upstream?

## Decision

**A new `InferenceReceipt` schema (`receipts/schema.py`), not an extension
of `InferenceEvent`.** They answer different questions for different
audiences: `InferenceEvent` is operational ("what happened, technically" —
for debugging), `InferenceReceipt` is economic ("what did this cost, and
to whom" — for the business). Keeping them separate means:

- `InferenceEvent`'s schema, sinks, and every existing test are completely
  untouched — zero regression risk on the feature this project already
  shipped and is trusted for.
- `InferenceEvent.estimated_cost_usd` is `float`. Populating it with a
  real, Decimal-derived cost would require a lossy `float(Decimal(...))`
  conversion right in the operational telemetry path — itself a violation
  of the money-correctness bar this feature exists to uphold. It stays
  permanently `null`; the field predates verified pricing and is
  superseded by `InferenceReceipt.estimated_cost_usd`, which is `Decimal`.
- The two records can be sent to different sinks with different retention
  policies later without a schema migration on either.

**Pricing: built-in catalog + operator override, `Decimal` throughout.**
`pricing/builtin.py` ships a small number of prices, each independently
verified against OpenAI's own published pricing page (not a blog,
aggregator, or a remembered value) with a recorded `source` and
`verified_date`. `inferrail.yaml`'s new `pricing:` section lets an
operator add or override any (provider, model) price, with the same
mandatory provenance fields. **The built-in catalog only auto-applies to a
provider configured as `type: openai` with no custom `base_url`** — i.e.
verifiably OpenAI's own API. An `openai_compatible` endpoint (vLLM, a
proxy, a self-hosted server) speaks the same wire format but could be
running a completely different, differently-priced model under a name
that happens to collide with a real OpenAI model id (e.g. someone serving
a local model literally named `gpt-4o-mini`); silently applying OpenAI's
price there would be exactly the kind of guessed-price fabrication this
project refuses to do (see docs/PRODUCT.md's non-goals, "F. Model mismatch
must fail visibly" in this feature's build brief). Any (provider, model)
that resolves to nothing is `None` — never `0` — both in the receipt and
in `inferrail report`'s aggregation.

**Attribution: `X-Inferrail-Attribute-<Name>` HTTP headers, not a body
field.** The OpenAI request body stays exactly what an unmodified OpenAI
client sends (docs/adr/0001). Attribution never enters
`NormalizedChatRequest` — the only type `OpenAIProvider.complete` accepts —
so it is structurally, not just procedurally, incapable of being forwarded
to the upstream provider. Values are collected into a generic
`dict[str, str]`, deliberately not a fixed `customer`/`workflow` schema:
new dimensions (tenant, environment, project, ...) need no code change.
Attribute values ARE persisted verbatim in `InferenceReceipt.attributes` —
this is the one intentional exception to "no payload content," since it's
caller-declared business metadata, not extracted from the prompt.

## Consequences

- `inferrail.yaml` gains two new optional sections, `receipts:` and
  `pricing:`, both with defaults — existing config files keep working
  unchanged.
- Unlike `telemetry:` (default: console), `receipts:` defaults to a local
  JSONL file (`./inferrail-receipts.jsonl`): a receipt's only purpose is
  to be aggregated later by `inferrail report`, so a console-only default
  would be a dead end for the exact workflow this feature exists to
  enable.
- A receipt is emitted for every execution attempt, success or failure —
  mirroring `InferenceEvent`'s existing behavior — so `inferrail report`
  can show a complete request count even though only successful requests
  with known pricing contribute to the cost total.
- Extending pricing to a second real provider later means adding entries
  to `pricing/builtin.py` (each independently verified, sourced, dated)
  and, if that provider's wire format differs from OpenAI's, a new
  eligibility rule in `PricingResolver` parallel to the `type: openai`
  check above — not a rewrite of the resolver's shape.
