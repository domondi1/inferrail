"""Run Inferrail Hosted inside the existing trial gateway's process (one Render service).

    /          -> hosted/cost_gateway (the /try/ trial API, unchanged)
    /hosted/*  -> hosted/budget_gateway (Inferrail Hosted)
    /read/*    -> hosted/web_read (Inferrail Web Read), only when READ_PAY_TO is set
    /openapi.json -> Hosted's operations, for agent catalogs (see origin_openapi)

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
import logging
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
READ_PREFIX = "/read"
log = logging.getLogger("inferrail.cohost")


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
    web_read: Any = "env",
) -> Starlette:
    trial_mod, hosted_mod = modules or load_modules()
    if web_read == "env":
        web_read = web_read_from_env(hosted_settings)
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

    routes = [Route("/openapi.json", openapi), Mount(PREFIX, app=hosted_app)]
    if web_read is not None:
        routes.append(Mount(READ_PREFIX, app=web_read[0]))
    app = Starlette(routes=[*routes, Mount("/", app=trial_app)], lifespan=lifespan)
    app.state.trial_app, app.state.hosted_app = trial_app, hosted_app
    app.state.hosted_settings = hosted_settings
    # Another app sharing this origin adds its paid operations here: callables returning
    # OpenAPI `paths` entries keyed by full origin path.
    app.state.discovery_paths = []
    if web_read is not None:
        app.state.discovery_paths.append(lambda: _web_read_paths(web_read[1]))
    return app


def web_read_from_env(hosted_settings: Any, env: dict[str, str] | None = None) -> Any:
    """(app, settings) for Inferrail Web Read when READ_PAY_TO is set, else None.

    Fail-isolated: a Web Read configuration error is logged and Web Read stays unmounted, so it
    can never take Hosted or the trial down. Its receiving address must differ from Hosted's and
    Search's: an EIP-3009 transfer binds to a merchant and amount, not an endpoint.
    """
    e = dict(os.environ if env is None else env)
    if not e.get("READ_PAY_TO"):
        return None
    try:
        mod = _load("web_read_service", HOSTED_DIR.parent / "web_read" / "service.py")
        origin = hosted_settings.public_base_url.removesuffix(PREFIX)
        e.setdefault("READ_RESOURCE_URL", f"{origin}{READ_PREFIX}/v1/page")
        e["READ_MOUNT_PREFIX"] = READ_PREFIX
        e.setdefault("READ_NETWORK", hosted_settings.x402_network)
        e.setdefault("READ_DATA_DIR", str(Path(hosted_settings.data_dir).parent / "web-read"))
        settings = mod.Settings.from_env(e)
        taken = {a.lower() for a in (hosted_settings.x402_pay_to, e.get("SEARCH_PAY_TO")) if a}
        if settings.pay_to.lower() in taken:
            raise RuntimeError("READ_PAY_TO must differ from Hosted's and Search's payTo")
        return mod.create_app(settings), settings
    except Exception:
        log.exception("Web Read not mounted: configuration error")
        return None


def _web_read_paths(settings: Any) -> dict[str, Any]:
    mod = sys.modules["web_read_service"]
    return {
        f"{READ_PREFIX}/v1/page": {
            "post": {
                "summary": "Read a public web page as clean markdown (pay per delivered page)",
                "description": mod.DESCRIPTION,
                "security": [],
                "x-payment-info": {
                    "price": {"mode": "fixed", "currency": "USD", "amount": settings.price_usd},
                    "protocols": [{"x402": {}}],
                },
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": mod.PageRequest.model_json_schema()}
                    },
                },
                "responses": {
                    "200": {
                        "description": "The page, delivered and paid",
                        "content": {"application/json": {"schema": mod.OUTPUT_SCHEMA}},
                    },
                    "402": {"description": "Payment Required"},
                    "422": {"description": "Not readable (see error); nothing charged"},
                    "502": {"description": "Upstream failure; nothing charged"},
                },
            }
        }
    }


def origin_openapi(app: Starlette) -> dict[str, Any]:
    """The origin's `GET /openapi.json`: what agent catalogs index for this origin.

    x402scan registers an endpoint only if it's listed here and reads its price from
    `x-payment-info`; AgentCash indexes every operation listed here for search. So the
    document lists only what an outside agent should call: Hosted's operations, with the
    workspace key and governance headers each one needs. The trial's operations (`/v1/...`
    at the root, a separate free product used from the /try/ page) used to be listed too;
    catalogs described them with Hosted's text, which sent agents to the trial's
    `/v1/chat/completions` with the wrong key. The trial still serves its own routes.
    """
    settings = app.state.hosted_settings
    base = settings.public_base_url
    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "Inferrail Hosted",
            "version": "1",
            "description": (
                "One dollar budget shared by every agent and process that uses a workspace "
                "key, reserved before each OpenAI or Anthropic call, so parallel agents can't "
                f"overspend a run. Create a workspace free with POST {base}/v1/workspaces; "
                f"{settings.free_runs_per_month} governed runs a month are free. Send calls "
                f"to {base}/v1/chat/completions (OpenAI shape) or {base}/v1/messages "
                "(Anthropic shape) with your own provider key, a work id and a ceiling. "
                f"More runs: USDC over x402 at POST {base}/v1/credits/x402."
            ),
        },
        "x-agentcash-guidance": {"llmsTxtUrl": base + "/llms.txt"},
        "components": {
            "securitySchemes": {
                "WorkspaceKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "x-api-key",
                    "description": f"Workspace key from POST {base}/v1/workspaces. "
                    "'Authorization: Bearer <key>' also works.",
                }
            }
        },
        "paths": _hosted_paths(settings),
    }
    for provider in app.state.discovery_paths:
        doc["paths"].update(provider())
    return doc


_GOVERNANCE_HEADERS = [
    {
        "name": "X-Provider-Api-Key",
        "in": "header",
        "required": True,
        "schema": {"type": "string"},
        "description": "Your own OpenAI or Anthropic key. Sent per call, never stored.",
    },
    {
        "name": "X-Inferrail-Attribute-Work-Id",
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
        "description": "Run id. Every agent and process using the same id shares one budget.",
    },
    {
        "name": "X-Inferrail-Budget-Usd",
        "in": "header",
        "required": False,
        "schema": {"type": "string", "example": "0.50"},
        "description": "Ceiling for the run in USD (max 100). The first value given for a "
        "work id is kept.",
    },
]


def _governed_call(summary: str, required: list[str]) -> dict[str, Any]:
    return {
        "post": {
            "summary": summary,
            "description": "Reserves the call's worst-case cost against the run's shared "
            "ceiling before it is sent to the provider; a call that would exceed it is refused "
            "and never reaches the provider. Provider spend is billed to your own key.",
            "security": [{"WorkspaceKey": []}],
            "parameters": _GOVERNANCE_HEADERS,
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "required": required,
                            "properties": {name: {} for name in required},
                        }
                    }
                },
            },
            "responses": {
                "200": {"description": "The provider's response, unchanged"},
                "401": {"description": "Missing or invalid workspace key"},
                "402": {
                    "description": "Refused before the provider: the run's ceiling, or the "
                    "workspace's free runs and credits, would be exceeded. Not an x402 "
                    "payment request."
                },
            },
        }
    }


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
    paths[f"{PREFIX}/v1/chat/completions"] = _governed_call(
        "Governed OpenAI-shaped chat completion (shared run budget)", ["model", "messages"]
    )
    paths[f"{PREFIX}/v1/messages"] = _governed_call(
        "Governed Anthropic-shaped message (shared run budget)",
        ["model", "max_tokens", "messages"],
    )
    paths[f"{PREFIX}/v1/workspace"] = {
        "get": {
            "summary": "Workspace usage: governed runs this month, free runs and credits left",
            "security": [{"WorkspaceKey": []}],
            "responses": {"200": {"description": "Usage"}, "401": {"description": "Bad key"}},
        }
    }
    paths[f"{PREFIX}/v1/work/{{work_id}}"] = {
        "get": {
            "summary": "One run's calls, cost and receipts",
            "security": [{"WorkspaceKey": []}],
            "parameters": [
                {"name": "work_id", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "responses": {"200": {"description": "The run"}, "401": {"description": "Bad key"}},
        }
    }
    if not settings.x402_pay_to:
        return paths
    paths[f"{PREFIX}/v1/credits/x402"] = {
        "post": {
            "summary": f"Buy {settings.x402_pack_runs:,} governed runs of Inferrail Hosted",
            "description": "Pay with USDC over x402. The 402 challenge needs no key; the paid "
            "request must carry the workspace key the runs are credited to. Each workspace "
            f"gets {settings.free_runs_per_month} governed runs a month free, so buy only once "
            "they are used up (GET /hosted/v1/workspace shows what is left). Using the runs "
            "requires your own OpenAI or Anthropic key, sent per call and never stored.",
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
