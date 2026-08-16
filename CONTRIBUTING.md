# Contributing to Inferrail

Inferrail is early, pre-1.0 software (see [README.md](README.md) and
[docs/PRODUCT.md](docs/PRODUCT.md) for exact scope). Contributions are
welcome, but please open an issue before starting anything larger than a
small fix — especially anything touching routing, the provider protocol,
or the telemetry schema, since those are deliberate architectural
boundaries (see `docs/adr/`).

## Development setup

```bash
git clone https://github.com/domondi1/inferrail.git
cd inferrail
pip install -e ".[dev]"
```

## Before opening a PR

Run the same checks CI runs:

```bash
ruff check .
mypy src
pytest
```

All of these must pass. `pytest` alone requires no API key or network
access — the one real-provider integration test in `tests/integration/` is
automatically skipped unless `OPENAI_API_KEY` is set, and it should stay
that way; don't make the default test suite depend on real credentials.

## Design principles this project holds to

- **No prompt/response payload persistence by default** — the telemetry
  schema structurally has no field for it (see
  `docs/adr/0003-no-payload-persistence-by-default.md`). Don't add one
  without a deliberate, separately-reviewed decision.
- **The OSS data plane requires no Inferrail-hosted service** (see
  `docs/adr/0004-data-plane-control-plane-boundary.md`). Don't add a code
  path that calls out to an Inferrail-operated backend.
- **Unsupported request features fail loudly, never silently** (e.g.
  `stream=true` today). If you add a feature, either implement it fully or
  keep rejecting it explicitly — don't let a client believe a parameter
  worked when it was ignored.
- Keep routing static and explicit for now — see
  `docs/adr/0002-static-deterministic-routing.md` for why, and don't build
  cost/latency-aware routing without a separate design discussion.

## Reporting bugs vs. security issues

Regular bugs: open a GitHub issue. Anything security- or privacy-sensitive
(credential handling, auth bypass, telemetry leaking payload data): see
[SECURITY.md](SECURITY.md) instead — please don't file those as public
issues first.
