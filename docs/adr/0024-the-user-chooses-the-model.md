# 0024. The user chooses the model; Inferrail prices it or refuses to budget it

## Status

Accepted (pending review)

## Context

The zero-config paths (`inferrail serve --quickstart`, `inferrail.start()`,
`inferrail try`) built a named route `default → gpt-4o-mini`. Explicit
model ids already passed through (ADR 0007), but three things made one
OpenAI model look like the product's model:

- a request that sent `model="default"` was silently sent to
  `gpt-4o-mini`;
- `inferrail try` called `gpt-4o-mini` unless told otherwise;
- the quickstart banner and help text named that model.

When that model is retired, all three break without any change on the
user's side. Separately, a new provider model can be called through
Inferrail without a release (pass-through), but can't run under a dollar
budget until it has a price, and the zero-config path had no way to
supply one without writing `inferrail.yaml`.

## Decision

1. **No model is chosen for the user.** A config may have no named routes
   when it has a `default_provider` (or `default_anthropic_provider`):
   every request's model id is passed through as-is. The quickstart
   config builds no route unless the caller names a model.
2. **`inferrail.start(model=...)` and `build_quickstart_config(model=...)`
   only define an alias**: `model="default"` then means the model the
   user named. Requests that name a model are unaffected.
3. **`inferrail try --model` is required.**
4. **`inferrail models [--provider NAME] [--config PATH] [--json]`** lists
   the models each configured provider's own list endpoint returns
   (OpenAI `GET /models`; Anthropic `GET /models`, paginated; compatible
   upstreams best effort, reported when unsupported), with each model's
   pricing status: built-in (verified date), operator-supplied,
   operator-asserted (`price_as`), or no price. It never selects a model
   and stores nothing.
5. **Pricing stays a separate trust boundary.** "The provider lists it"
   never implies a price. Exact-match lookup only (ADR 0005, resolver):
   built-in catalog for the vendor's own API, operator overrides, or an
   operator-asserted `price_as`. An unpriced model passes through with
   cost unknown, and a block budget refuses it (`INFERRAIL_E012`).
   `inferrail.start(pricing={provider: {model: PriceEntry}})` lets a
   zero-config user supply a price (source and verified date required),
   recorded as operator-supplied.

## Consequences

- A newly released model needs no Inferrail change to be called, and
  either a release (catalog) or an operator price to be budgeted.
- A retired model needs no Inferrail change; its catalog entry is
  harmless until removed.
- Behaviour change: on the zero-config paths, a request that sends
  `model="default"` now reaches the provider as `default` (an error from
  the provider) unless `model=` was given. `inferrail try` without
  `--model` is a usage error. Configs with routes are unaffected.
- Concrete model names remain in tests and benchmarks (reproducibility)
  and in docs as labelled examples.
