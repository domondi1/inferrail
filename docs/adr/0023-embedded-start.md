# 0023. `inferrail.start()`: run the existing gateway inside the caller's process

## Status

Accepted (pending review)

## Context

Protecting one agent run needed: install, write `inferrail.yaml`, export
the provider key in a second terminal, run `inferrail serve`, keep it
running, then change the app's base URL and add two headers. Most of
that is about running a separate process, not about budgets. For someone
trying Inferrail inside an app they already have, the second process is
the step most likely to stop them.

The zero-config pieces already existed: `build_quickstart_config()`
(ADR 0020) and the app-data paths used by `inferrail serve --app-mode`
(ADR 0016). What was missing was a way to run them without a terminal,
and a way for `inferrail work <id>` to find the receipts afterwards.

## Decision

### `inferrail.start()` / `inferrail.stop()`

`inferrail.start(config=None, *, host="127.0.0.1", port=0, model=None)`
builds a config, calls the same `create_app()` that `inferrail serve`
uses, and runs it with `uvicorn.Server` on a daemon thread. It returns
the base URL (`http://127.0.0.1:<port>/v1`).

- **No second implementation.** Routing, pricing, budgets, the atomic
  reservation (ADR 0021), declared budgets (ADR 0022), field policy,
  streaming, receipts and refusals are the gateway's own code paths. The
  module only builds a config and runs the server.
- **Zero-config by default.** With no `config`, it uses the quickstart
  providers (OpenAI and Anthropic, keys from the process environment),
  telemetry off, and SQLite receipts and budgets at the app-data paths
  `--app-mode` uses (`<app data>/receipts.db`, `budgets.db`). A path or
  an `InferrailConfig` can be passed to use an operator's config instead.
- **Port:** `0` by default, so the OS assigns a free port at bind time
  and the bound port is read back from the socket. No fixed default
  port to collide with another process or a second app.
- **Repeat calls:** one gateway per process. The same arguments return
  the same URL; different arguments while one is running raise
  `RuntimeError` (call `stop()` first). A failure to bind raises and
  leaves nothing registered.
- **Shutdown:** `stop()` sets uvicorn's exit flag and joins the thread
  (5 s cap), letting in-flight requests finish; it's registered with
  `atexit` and is a no-op when nothing runs. The thread is a daemon, so
  it never keeps a process alive.
- The Anthropic SDK takes the URL without the trailing `/v1`.

### `inferrail work` finds zero-config receipts

When no `--receipts`, `--config` or `./inferrail.yaml` is present, the
receipts lookup (`report`, `transaction`, `work`) now considers, in
order: `./inferrail-receipts.jsonl` (unchanged behaviour when it exists)
and the app-data `receipts.db`. `inferrail work <id>` uses the first of
those that has receipts for `<id>`, so an old quickstart file in the
current directory doesn't hide a run recorded by `inferrail.start()`.

## Consequences

- The enforcement point moves into the caller's process. It is still
  outside the agent's own logic (an HTTP hop every model call takes),
  but it isn't process isolation: code in the same process could call
  the provider directly. `inferrail serve` remains the option when that
  matters, and for a gateway shared by several services.
- Several worker processes on one host each start their own embedded
  gateway against the same SQLite files; the reservation transaction
  (`BEGIN IMMEDIATE`) keeps admission atomic across them.
- Gateway log lines (for example a refused request) now print in the
  caller's process output.
- Not for environments without a writable user data directory or with
  no persistent disk (most serverless functions).
