# Inferrail — Agent Context

Inferrail is an open inference control plane: an OpenAI-compatible
gateway that sits between an application and the model providers it
calls. **v0.1 today is the gateway only.** See `docs/PRODUCT.md` for
exact current scope and `docs/ARCHITECTURE.md` for how it's built.

## Before major feature work, read

- `docs/PRODUCT.md` — authoritative source for exact current scope: what
  works, what's explicitly not supported yet
- `docs/ARCHITECTURE.md` — package layout, request lifecycle, and the
  boundaries (provider, routing, telemetry, data-plane/control-plane)
  that keep the codebase easy to extend
- `docs/PRINCIPLES.md` — the durable engineering principles behind this
  project
- `docs/adr/` — architecture decision records for specific structural
  choices

## Before implementing something substantial, ask

1. Which problem does this change solve?
2. Does it match a principle in `docs/PRINCIPLES.md`, or does it conflict
   with one?
3. Is there a smaller implementation?
4. Does `docs/PRODUCT.md` need to change alongside the code, so scope
   stays accurate?
5. What test proves this works?

## Engineering rules

- Exact current scope must match the code. If a feature ships or a
  non-goal changes, update `docs/PRODUCT.md` in the same change.
- No prompt/response payload persistence in telemetry by default — this
  is a schema-level guarantee, not a runtime flag.
- No secret or silent telemetry — nothing leaves the machine unless a
  sink is explicitly configured to do that, and it must be documented.
- No fabricated metrics. Values that can't be measured trustworthily
  (cost, time-to-first-token, etc.) stay `null` — never estimated or
  guessed.
- The OSS data plane must keep working with zero dependency on any
  Inferrail-operated service.
- Prefer deterministic, testable behavior — keep the core engine
  independent of the web framework so it can be tested without an HTTP
  server.
- Keep the hot request-execution path small; avoid unnecessary
  dependencies and speculative complexity.
- Clearly distinguish shipped features from future work — a missing
  capability is a documented non-goal, not a silent gap.

## Repo-level conventions

- Python 3.11+, FastAPI, pydantic, strict mypy, ruff. `pytest` runs fully
  mocked — no network/API key required except the one skipped integration
  test.
- Package layout and request lifecycle: `docs/ARCHITECTURE.md`.
- Run lint, type-check, and the test suite before considering work
  complete.
- Money-related code (anything cost/pricing/budget) is held to a higher
  correctness bar than the rest of the codebase: never silently
  double-count, lose, or fabricate a cost value, and represent
  uncertainty explicitly rather than collapsing it into a guess.
