# Inferrail Economic Authority (hosted) — Phase A

**Status: core engine only.** This directory currently contains only the
durable economic-authority core (`core.py`): reserve/grant/consume/settle
over a delegated spending ceiling, with conservation and idempotency
guarantees enforced by SQLite.

**Not yet present, by design at this stage:** any network transport (no
A2A adapter yet), any payment/x402 wiring, any authorization/credential
layer, and any deployment configuration. There is nothing to run as a
service yet — `core.py` is a library module, exercised directly by the
tests in `tests/unit/hosted/test_a2a_economic_authority_core.py`.

Like `hosted/work_economics/`, this directory is not part of the
`inferrail` package: it is not imported by any gateway code path, is not
included in the wheel build (see `pyproject.toml`'s
`[tool.hatch.build.targets.wheel]` package list), and `inferrail serve`
has no dependency on it. See `docs/adr/0004-data-plane-control-plane-boundary.md`
for the general principle this follows.
