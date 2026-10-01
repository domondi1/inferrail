"""Run the Inferrail gateway inside your own Python process.

`inferrail.start()` is the same gateway `inferrail serve` runs, started on
a background thread on a free local port, so trying Inferrail doesn't
need a second terminal or a config file:

    import inferrail
    base_url = inferrail.start()        # http://127.0.0.1:<port>/v1

Point any OpenAI-compatible client at `base_url` and send the per-run
headers as usual. Nothing about admission, pricing, budgets, receipts,
streaming or refusals is different: this module only builds the config,
calls `create_app` and runs it with uvicorn. See
docs/adr/0023-embedded-start.md.

With no `config`, it uses the quickstart providers (OpenAI and Anthropic,
keys read from this process's environment) and stores receipts and
budgets in the same app-data files `inferrail serve --app-mode` uses, so
`inferrail work <run-id>` finds them without extra flags.
"""

from __future__ import annotations

import atexit
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from inferrail.appdata import ensure_app_data_dir
from inferrail.config.models import BudgetsConfig, InferrailConfig, ReceiptsConfig

_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 5.0


@dataclass
class _Running:
    key: tuple[object, ...]
    base_url: str
    server: object  # uvicorn.Server
    thread: threading.Thread


_lock = threading.Lock()
_running: _Running | None = None


def _build_config(
    config: str | Path | InferrailConfig | None, model: str | None
) -> InferrailConfig:
    if isinstance(config, InferrailConfig):
        return config
    if config is not None:
        from inferrail.config.loader import load_config

        return load_config(config)
    from inferrail.config.quickstart import QUICKSTART_MODEL, build_quickstart_config

    built = build_quickstart_config(model=model or QUICKSTART_MODEL, telemetry_sink="none")
    app_data = ensure_app_data_dir()
    # Same files as `inferrail serve --app-mode`, so `inferrail work`, the
    # dashboard and `inferrail budget` all see this process's runs.
    built.receipts = ReceiptsConfig(sink="sqlite", path=str(app_data / "receipts.db"))
    built.budgets = BudgetsConfig(enabled=True, path=str(app_data / "budgets.db"))
    return built


def start(
    config: str | Path | InferrailConfig | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    model: str | None = None,
) -> str:
    """Start the gateway on a background thread and return its base URL
    (ending in ``/v1``).

    - ``config``: path to an ``inferrail.yaml`` or an ``InferrailConfig``.
      Default: zero-config quickstart with budgets on and receipts in the
      app-data directory.
    - ``port``: ``0`` (default) lets the OS pick a free port. Pass a fixed
      port only if something else needs to know it in advance.
    - ``model``: the quickstart route's model (zero-config only).

    Calling it again with the same arguments returns the same URL. Calling
    it with different arguments while a gateway is running raises
    ``RuntimeError``; call :func:`stop` first. The gateway stops when the
    process exits, or when you call :func:`stop`.
    """
    global _running
    key = (
        str(config) if not isinstance(config, InferrailConfig) else id(config),
        host,
        port,
        model,
    )
    with _lock:
        if _running is not None:
            if _running.thread.is_alive() and _running.key == key:
                return _running.base_url
            if _running.thread.is_alive():
                raise RuntimeError(
                    "inferrail.start() is already running with different arguments "
                    f"at {_running.base_url}; call inferrail.stop() first"
                )
            _running = None

        import uvicorn

        from inferrail.gateway.app import create_app

        app = create_app(_build_config(config, model))
        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        )
        errors: list[BaseException] = []

        def _serve() -> None:
            try:
                server.run()
            except BaseException as exc:  # surfaced to the caller below
                errors.append(exc)

        thread = threading.Thread(target=_serve, name="inferrail-gateway", daemon=True)
        thread.start()
        deadline = time.monotonic() + _START_TIMEOUT_S
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                server.should_exit = True
                detail = f": {errors[0]}" if errors else ""
                raise RuntimeError(f"Inferrail gateway failed to start on {host}:{port}{detail}")
            time.sleep(0.01)
        bound_port = server.servers[0].sockets[0].getsockname()[1]
        url_host = f"[{host}]" if ":" in host else host
        _running = _Running(key, f"http://{url_host}:{bound_port}/v1", server, thread)
        return _running.base_url


def stop() -> None:
    """Stop the gateway started by :func:`start`, waiting for in-flight
    requests to finish (up to a few seconds). Safe to call when nothing
    is running."""
    global _running
    with _lock:
        running, _running = _running, None
    if running is None:
        return
    running.server.should_exit = True  # type: ignore[attr-defined]
    running.thread.join(_STOP_TIMEOUT_S)


atexit.register(stop)
