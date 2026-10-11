"""Run Inferrail Hosted inside the existing trial gateway's process (one Render service).

    /          -> hosted/cost_gateway (the /try/ trial API, unchanged)
    /hosted/*  -> hosted/budget_gateway (Inferrail Hosted)

Why one process: it reuses an already-paid instance. Both apps are small (measured in this
module's tests and the PR). Isolation holds in one process because:
- **separate code:** each app is loaded from its own directory under its own module name, and
  the trial app never imports payment code;
- **separate state:**
  - trial data stays in `COST_GATEWAY_DATA_DIR` (ephemeral by design: trials expire);
  - Hosted's money-critical state stays in `BG_DATA_DIR`, which must be the persistent disk;
- **separate credentials:** trial bearer tokens are unknown to Hosted, and Hosted workspace keys
  are unknown to the trial;
- **separate lifecycles:** both apps' startup/shutdown (the trial purge loop, Hosted's chain
  reconciliation) run under one combined lifespan.

The single-instance guarantee is unchanged: Hosted's `flock` on `BG_DATA_DIR` refuses a second
process. Run with one worker:

    python hosted/budget_gateway/cohost.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

from starlette.applications import Starlette
from starlette.routing import Mount

HOSTED_DIR = Path(__file__).resolve().parent
TRIAL_DIR = HOSTED_DIR.parent / "cost_gateway"
PREFIX = "/hosted"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TRIAL_FLAT_MODULES = ("trial", "keys", "tenant_store", "auth", "demo_provider")


def load_modules() -> tuple[ModuleType, ModuleType]:
    """(trial service module, hosted service module), each under a unique name. The trial's
    flat helper modules are loaded explicitly from its own directory, so a same-named module
    from another hosted service (e.g. ap_exceptions' `auth`) can never be picked up."""
    for name in TRIAL_FLAT_MODULES:
        _load(name, TRIAL_DIR / f"{name}.py")
    trial = _load("cost_gateway_service", TRIAL_DIR / "service.py")
    hosted = _load("budget_gateway_service", HOSTED_DIR / "service.py")
    return trial, hosted


def create_combined_app(
    trial_data_dir: Path,
    hosted_settings: Any,
    *,
    facilitator: Any | None = None,
    modules: tuple[ModuleType, ModuleType] | None = None,
) -> Starlette:
    trial_mod, hosted_mod = modules or load_modules()
    trial_app = trial_mod.create_app(trial_data_dir)
    hosted_app = hosted_mod.create_app(hosted_settings, facilitator=facilitator)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(trial_app.router.lifespan_context(trial_app))
                await stack.enter_async_context(hosted_app.router.lifespan_context(hosted_app))
                yield
        finally:
            hosted_app.state.release_instance_lock()

    app = Starlette(
        routes=[Mount(PREFIX, app=hosted_app), Mount("/", app=trial_app)], lifespan=lifespan
    )
    app.state.trial_app, app.state.hosted_app = trial_app, hosted_app
    return app


def settings_from_env(hosted_mod: ModuleType) -> Any:
    env = dict(os.environ)
    env.setdefault("BG_MOUNT_PREFIX", PREFIX)
    if env["BG_MOUNT_PREFIX"] != PREFIX:
        raise RuntimeError(f"BG_MOUNT_PREFIX must be {PREFIX} when cohosted")
    return hosted_mod.Settings.from_env(env)


if __name__ == "__main__":
    import uvicorn

    mods = load_modules()
    mods[0].configure_logging()
    application = create_combined_app(
        Path(os.environ.get("COST_GATEWAY_DATA_DIR", "/tmp/inferrail_cost_gateway")),
        settings_from_env(mods[1]),
        modules=mods,
    )
    uvicorn.run(
        application,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8423")),
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="*",
        workers=1,
    )
