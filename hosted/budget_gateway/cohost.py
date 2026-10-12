"""Run Inferrail Hosted inside the existing trial gateway's process (one Render service).

    /          -> hosted/cost_gateway (the /try/ trial API, unchanged)
    /hosted/*  -> hosted/budget_gateway (Inferrail Hosted)
    /openapi.json -> the trial's schema plus Hosted's purchasable operations (see below)

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
import copy
import importlib.util
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

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

    async def openapi(_: Request) -> JSONResponse:
        return JSONResponse(origin_openapi(app))

    app = Starlette(
        routes=[
            Route("/openapi.json", openapi),
            Mount(PREFIX, app=hosted_app),
            Mount("/", app=trial_app),
        ],
        lifespan=lifespan,
    )
    app.state.trial_app, app.state.hosted_app = trial_app, hosted_app
    app.state.hosted_settings = hosted_settings
    # Another app sharing this origin adds its paid operations here: callables returning
    # OpenAPI `paths` entries keyed by full origin path.
    app.state.discovery_paths = []
    return app


def origin_openapi(app: Starlette) -> dict[str, Any]:
    """The origin's `GET /openapi.json`.

    x402 catalogs (x402scan, which feeds AgentCash) discover an origin only through this
    document: they register an endpoint only if it's listed here, and read its price from
    `x-payment-info`. Without it the trial's schema answered alone, so neither Hosted
    operation was discoverable. The trial's own paths stay unchanged, so its /docs still work.
    """
    settings = app.state.hosted_settings
    doc = copy.deepcopy(app.state.trial_app.openapi())
    doc["info"]["title"] = "Inferrail"
    doc["info"]["description"] = (
        "Inferrail Hosted: one dollar budget shared by every agent and process using a "
        "workspace key, reserved before each LLM provider call, so parallel agents can't "
        f"overspend. Create a workspace free with POST {PREFIX}/v1/workspaces, then buy "
        f"governed runs with USDC over x402 at POST {PREFIX}/v1/credits/x402."
    )
    doc["x-agentcash-guidance"] = {"llmsTxtUrl": settings.public_base_url + "/llms.txt"}
    schemes = doc.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes["WorkspaceKey"] = {
        "type": "apiKey",
        "in": "header",
        "name": "x-api-key",
        "description": f"Workspace key from POST {PREFIX}/v1/workspaces. "
        "'Authorization: Bearer <key>' also works.",
    }
    paths = doc.setdefault("paths", {})
    paths.update(_hosted_paths(settings))
    for provider in app.state.discovery_paths:
        paths.update(provider())
    return doc


def _hosted_paths(settings: Any) -> dict[str, Any]:
    paths: dict[str, Any] = {
        f"{PREFIX}/v1/workspaces": {
            "post": {
                "summary": "Create an Inferrail Hosted workspace (free)",
                "description": "Returns a workspace key, shown once. Every client using the key "
                "shares one dollar budget, reserved before each provider call. "
                f"{settings.free_runs_per_month} governed runs a month are free.",
                "security": [],
                "responses": {
                    "201": {
                        "description": "Workspace created",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "workspace_id": {"type": "string"},
                                        "api_key": {"type": "string"},
                                        "base_url": {"type": "string"},
                                    },
                                    "required": ["workspace_id", "api_key", "base_url"],
                                }
                            }
                        },
                    },
                    "429": {"description": "Too many workspaces from this address"},
                    "503": {"description": "Workspace creation paused or at capacity"},
                },
            }
        }
    }
    if not settings.x402_pay_to:
        return paths
    paths[f"{PREFIX}/v1/credits/x402"] = {
        "post": {
            "summary": f"Buy {settings.x402_pack_runs:,} governed runs of Inferrail Hosted",
            "description": "Pay with USDC over x402. The 402 challenge needs no key; the paid "
            "request must carry the workspace key the runs are credited to.",
            "security": [{"WorkspaceKey": []}],
            "x-payment-info": {
                "price": {
                    "mode": "fixed",
                    "currency": "USD",
                    "amount": settings.x402_pack_price_usd,
                },
                "protocols": [{"x402": {}}],
            },
            "requestBody": {
                "required": False,
                "content": {"application/json": {"schema": {"type": "object", "properties": {}}}},
            },
            "responses": {
                "200": {
                    "description": "Payment settled; runs credited",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "workspace_id": {"type": "string"},
                                    "governed_runs": {"type": "integer"},
                                    "payment_ref": {"type": "string"},
                                    "status": {"type": "string", "enum": ["settled"]},
                                    "next": {"type": "string"},
                                },
                                "required": ["workspace_id", "governed_runs", "status"],
                            }
                        }
                    },
                },
                "401": {"description": "Missing or invalid workspace key"},
                "402": {"description": "Payment Required"},
            },
        }
    }
    return paths


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
