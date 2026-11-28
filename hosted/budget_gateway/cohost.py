"""Run Inferrail Hosted inside the existing trial gateway's process (one Render service).

    /          -> hosted/cost_gateway (the /try/ trial API, unchanged)
    /hosted/*  -> hosted/budget_gateway (Inferrail Hosted)
    /read/*    -> hosted/web_read (Inferrail Web Read), only when READ_PAY_TO is set
    /search/*  -> hosted/web_read/search.py (Inferrail Web Search, a Web Read task), only when
                  WEB_SEARCH_PAY_TO and its supplier approvals are set
    /ops/*     -> hosted/web_read/godmode.py (the operator's private commercial view), only when
                  GODMODE_LOGIN_SECRET and READ_COMMERCE_EVENTS=1 are set; never listed
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

import asyncio
import contextlib
import hashlib
import hmac
import importlib.util
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route

HOSTED_DIR = Path(__file__).resolve().parent
TRIAL_DIR = HOSTED_DIR.parent / "cost_gateway"
PREFIX = "/hosted"
READ_PREFIX = "/read"
SEARCH_PREFIX = "/search"
FIND_PREFIX = "/find"
PKG_PREFIX = "/pkg"
DOMAIN_PREFIX = "/domain"
OPS_PREFIX = "/ops"
SITE = "https://tryinferrail.com"
REPO = "https://github.com/domondi1/inferrail"
MEMORY_LOG_S = 900  # one `memory` log line every 15 minutes
log = logging.getLogger("inferrail.cohost")


def process_memory() -> dict[str, int | None]:
    """This process's resident memory now and at its peak, in MB (Linux; None elsewhere)."""
    rss: int | None = None
    try:
        with open("/proc/self/statm") as f:
            rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 2**20
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource

        peak: int | None = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024  # KiB
    except (ImportError, OSError):
        peak = None
    # The two readings come from different counters (statm pages vs ru_maxrss KiB) and round
    # separately, so "now" can read 1 MB above "peak". The peak is never below the current value.
    if rss is not None and peak is not None:
        peak = max(peak, rss)
    return {"rss_mb": rss, "peak_rss_mb": peak}


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
    web_search: Any = "env",
    finder: Any = "env",
    packages: Any = "env",
    domains: Any = "env",
    godmode: Any = "env",
) -> Starlette:
    trial_mod, hosted_mod = modules or load_modules()
    hosted_app = hosted_mod.create_app(hosted_settings, facilitator=facilitator)
    read_credits = None
    if web_read == "env":
        read_credits = read_credits_from_env(hosted_app, hosted_settings)
        web_read = web_read_from_env(hosted_settings, credits=read_credits)
    read_batch = web_read_batch_from_env(hosted_settings) if web_read is not None else None
    if web_search == "env":
        web_search = web_search_from_env(hosted_settings)
    if finder == "env":
        finder = finder_from_env(hosted_settings)
    if packages == "env":
        packages = package_check_from_env(hosted_settings)
    if domains == "env":
        domains = domain_check_from_env(hosted_settings)
    if godmode == "env":
        godmode = godmode_from_env(web_read)
    trial_app = trial_mod.create_app(trial_data_dir)
    started = time.monotonic()

    def memory() -> dict[str, Any]:
        catalog = getattr(finder[0].state, "finder_catalog", None) if finder else None
        entries = len(catalog.entries) if catalog is not None else None
        return {**process_memory(), "finder_catalog_entries": entries}

    async def log_memory() -> None:
        """A `memory` line at startup and every MEMORY_LOG_S, so memory growth shows up in the
        host's logs long before it reaches the instance limit."""
        while True:
            try:
                trial_mod._log_event(logging.INFO, "memory", **memory())
            except Exception:  # noqa: BLE001 - observability never takes the service down
                pass
            await asyncio.sleep(MEMORY_LOG_S)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        memory_task = asyncio.get_running_loop().create_task(log_memory())
        try:
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(trial_app.router.lifespan_context(trial_app))
                await stack.enter_async_context(hosted_app.router.lifespan_context(hosted_app))
                mcp_server = getattr(web_read[0].state, "mcp", None) if web_read else None
                if mcp_server is not None:  # Web Read's paid MCP tool (READ_MCP=1)
                    await stack.enter_async_context(mcp_server.session_manager.run())
                yield
        finally:
            memory_task.cancel()
            hosted_app.state.release_instance_lock()

    async def operator_memory(request: Request) -> JSONResponse:
        """Operator only (BG_OPERATOR_TOKEN): process memory, for checking headroom."""
        expected = hosted_settings.operator_token
        if not expected:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token.encode(), expected.encode()):
            return JSONResponse({"detail": "operator token required"}, status_code=401)
        return JSONResponse({**memory(), "uptime_s": int(time.monotonic() - started)})

    async def openapi(_: Request) -> JSONResponse:
        return JSONResponse(origin_openapi(app))

    async def home(_: Request) -> HTMLResponse:
        return HTMLResponse(origin_home(app))

    async def api_catalog(_: Request) -> JSONResponse:
        return JSONResponse(origin_api_catalog(app), media_type="application/linkset+json")

    async def llms(_: Request) -> PlainTextResponse:
        return PlainTextResponse(origin_llms(app))

    async def favicon(_: Request) -> RedirectResponse:
        return RedirectResponse(SITE + "/favicon.ico", status_code=301)

    routes = [
        Route("/openapi.json", openapi),
        Route("/", home),
        Route("/llms.txt", llms),
        Route("/.well-known/api-catalog", api_catalog),
        Route("/favicon.ico", favicon),
        Route("/v1/operator/memory", operator_memory),
        Mount(PREFIX, app=hosted_app),
    ]
    if read_batch is not None:  # an exact path ahead of Web Read's mount, which it doesn't touch
        routes.append(Route(f"{READ_PREFIX}/v1/pages", endpoint=read_batch[0], methods=["POST"]))
    if web_read is not None:
        routes.append(Mount(READ_PREFIX, app=web_read[0]))
    if web_search is not None:
        routes.append(Mount(SEARCH_PREFIX, app=web_search[0]))
    if finder is not None:
        routes.append(Mount(FIND_PREFIX, app=finder[0]))
    if packages is not None:
        routes.append(Mount(PKG_PREFIX, app=packages[0]))
    if domains is not None:
        routes.append(Mount(DOMAIN_PREFIX, app=domains[0]))
    if godmode is not None:
        routes.append(Mount(OPS_PREFIX, app=godmode))
    app = Starlette(routes=[*routes, Mount("/", app=trial_app)], lifespan=lifespan)
    app.state.trial_app, app.state.hosted_app = trial_app, hosted_app
    app.state.hosted_settings = hosted_settings
    app.state.web_read_settings = web_read[1] if web_read is not None else None
    app.state.read_credits = web_read is not None and read_credits is not None
    app.state.web_read_batch_settings = read_batch[1] if read_batch is not None else None
    app.state.web_search_settings = web_search[1] if web_search is not None else None
    app.state.finder_settings = finder[1] if finder is not None else None
    app.state.package_settings = packages[1] if packages is not None else None
    app.state.domain_settings = domains[1] if domains is not None else None
    # Another app sharing this origin adds its paid operations here: callables returning
    # OpenAPI `paths` entries keyed by full origin path.
    app.state.discovery_paths = []
    if web_read is not None:
        app.state.discovery_paths.append(lambda: _web_read_paths(web_read[1]))
    if read_batch is not None:
        app.state.discovery_paths.append(lambda: _web_read_batch_paths(read_batch[1]))
    if web_search is not None:
        app.state.discovery_paths.append(lambda: _web_search_paths(web_search[1]))
    if finder is not None:
        app.state.discovery_paths.append(lambda: _finder_paths(finder[1]))
    if packages is not None:
        app.state.discovery_paths.append(lambda: _package_paths(packages[1]))
    if domains is not None:
        app.state.discovery_paths.append(lambda: _domain_paths(domains[1]))
    return app


class HostedCredits:
    """Web Read's view of Hosted's workspace ledger (credits_rail.Credits): authenticate a key,
    hold and finish a spend. Nothing else of the ledger is reachable from Web Read."""

    def __init__(self, ledger: Any, origin: str, *, units_per_result: int, card: bool) -> None:
        self._ledger = ledger
        self.units_per_result = units_per_result
        self.top_up = {
            "start": f"{origin}{PREFIX}/v1/start (browser: create a key and pay by card)",
            "create_key": f"POST {origin}{PREFIX}/v1/workspaces",
            "x402": f"POST {origin}{PREFIX}/v1/credits/x402 (USDC on Base, 1,000 credits = $1)",
        }
        if card:
            self.top_up["card"] = f"POST {origin}{PREFIX}/v1/credits/checkout"

    def authenticate(self, api_key: str) -> str | None:
        return self._ledger.authenticate(api_key)

    def hold(self, workspace_id: str, ref: str, units: int) -> tuple[str, int]:
        return self._ledger.hold_spend(workspace_id, ref, units)

    def finish(self, ref: str, delivered: bool) -> None:
        self._ledger.finish_spend(ref, delivered)


def read_credits_from_env(
    hosted_app: Any, hosted_settings: Any, env: dict[str, str] | None = None
) -> Any:
    """Hosted credits for Web Read when READ_CREDITS=1 (default off). A Web Read page costs
    READ_CREDITS_PER_PAGE credits (default 2: Hosted credits are $0.001, a page is $0.002)."""
    e = dict(os.environ if env is None else env)
    if e.get("READ_CREDITS") != "1":
        return None
    ledger = getattr(hosted_app.state, "ledger", None)
    if ledger is None:
        log.error("READ_CREDITS=1 but Hosted has no workspace ledger; credits rail off")
        return None
    origin = hosted_settings.public_base_url.removesuffix(PREFIX)
    per_page = int(e.get("READ_CREDITS_PER_PAGE", "2"))
    if per_page < 1:
        raise RuntimeError("READ_CREDITS_PER_PAGE must be at least 1")
    card = getattr(hosted_settings, "stripe", None) is not None
    return HostedCredits(ledger, origin, units_per_result=per_page, card=card)


def web_read_from_env(
    hosted_settings: Any, env: dict[str, str] | None = None, credits: Any = None
) -> Any:
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
        return mod.create_app(settings, credits=credits), settings
    except Exception:
        log.exception("Web Read not mounted: configuration error")
        return None


def web_read_batch_from_env(hosted_settings: Any, env: dict[str, str] | None = None) -> Any:
    """(app, settings) for Web Read's batch (`POST /read/v1/pages`) when READ_BATCH=1, else None.

    The same product, price per page and receiving addresses as Web Read (READ_* settings), with
    its own ledger. Sharing the address is safe because every payment is bound to one request:
    an x402 authorization's amount is the batch's exact price and its nonce settles once on
    chain (a reused one fails to settle and the result is withheld); an MPP challenge is bound
    to the route path and amount; the retry identity is namespaced and per payer. Off by
    default: turning it on is a production pricing change. Fail-isolated like Web Read."""
    e = dict(os.environ if env is None else env)
    if e.get("READ_BATCH") != "1" or not e.get("READ_PAY_TO"):
        return None
    try:
        mod = sys.modules.get("web_read_service") or _load(
            "web_read_service", HOSTED_DIR.parent / "web_read" / "service.py"
        )
        batch = _load("web_read_batch", HOSTED_DIR.parent / "web_read" / "batch.py")
        origin = hosted_settings.public_base_url.removesuffix(PREFIX)
        e["READ_RESOURCE_URL"] = f"{origin}{READ_PREFIX}/v1/pages"
        e["READ_MOUNT_PREFIX"] = ""  # the task's path is the full path (served by a Route)
        e.setdefault("READ_NETWORK", hosted_settings.x402_network)
        e["READ_DATA_DIR"] = str(
            Path(e.get("READ_BATCH_DATA_DIR") or Path(hosted_settings.data_dir).parent
                 / "web-read-batch")
        )  # fmt: skip
        e["READ_MCP"] = "0"
        e["READ_ALLOW_GET"] = "0"
        e["READ_AWS_PRODUCT_ID"] = ""  # AWS Agentic Commerce sells the single-page route only
        settings = mod.Settings.from_env(e)
        taken = {a.lower() for a in (hosted_settings.x402_pay_to, e.get("SEARCH_PAY_TO")) if a}
        if settings.pay_to.lower() in taken:
            raise RuntimeError("READ_PAY_TO must differ from Hosted's and Search's payTo")
        reader = mod.fetch.Fetcher(settings.user_agent, pdf=settings.pdf)
        return mod.create_app(settings, task=batch.batch_task(mod, reader)), settings
    except Exception:
        log.exception("Web Read batch not mounted: configuration error")
        return None


def godmode_from_env(web_read: Any, env: dict[str, str] | None = None) -> Any:
    """The private operator view (hosted/web_read/godmode.py) when GODMODE_LOGIN_SECRET is set,
    else None. It needs Web Read mounted with READ_COMMERCE_EVENTS=1 (the event history it
    shows). Fail-isolated like Web Read: a configuration error leaves it unmounted."""
    e = dict(os.environ if env is None else env)
    if not e.get("GODMODE_LOGIN_SECRET") or web_read is None:
        return None
    try:
        mod = _load("web_read_godmode", HOSTED_DIR.parent / "web_read" / "godmode.py")
        settings = mod.GodModeSettings.from_env(e)
        app, read_settings = web_read
        store = app.state.commerce_events
        if not hasattr(store, "append"):
            raise RuntimeError("God Mode needs READ_COMMERCE_EVENTS=1")
        return mod.create_app(store, app.state.ledger_path, read_settings, settings)
    except Exception:
        log.exception("God Mode not mounted: configuration error")
        return None


def web_search_from_env(hosted_settings: Any, env: dict[str, str] | None = None) -> Any:
    """(app, settings) for Inferrail Web Search when WEB_SEARCH_PAY_TO is set, else None.

    Sold only with written supplier permission (WEB_SEARCH_SUPPLIER_RIGHTS_CONFIRMED=1), the
    supplier key and its verified all-in cost per search (WEB_SEARCH_SUPPLIER_CREDIT_USD, from
    the paid invoice), at a price above that cost. MPP only unless WEB_SEARCH_X402=1, which
    needs a receiving address of its own (an EIP-3009 transfer binds a merchant and amount, not
    an endpoint). Fail-isolated like Web Read: a configuration error leaves it unmounted.
    """
    e = dict(os.environ if env is None else env)
    if not e.get("WEB_SEARCH_PAY_TO"):
        return None
    try:
        if e.get("WEB_SEARCH_SUPPLIER_RIGHTS_CONFIRMED") != "1":
            raise RuntimeError("Web Search needs WEB_SEARCH_SUPPLIER_RIGHTS_CONFIRMED=1")
        mod = sys.modules.get("web_read_service") or _load(
            "web_read_service", HOSTED_DIR.parent / "web_read" / "service.py"
        )
        search = _load("web_read_search", HOSTED_DIR.parent / "web_read" / "search.py")
        suppliers, contract = search.load_suppliers()
        if e.get("WEB_SEARCH_SUPPLIER", "serpex") != "serpex":
            raise RuntimeError("WEB_SEARCH_SUPPLIER must be serpex")
        credit = contract.atomic(e["WEB_SEARCH_SUPPLIER_CREDIT_USD"])
        origin = hosted_settings.public_base_url.removesuffix(PREFIX)
        e.setdefault("WEB_SEARCH_RESOURCE_URL", f"{origin}{SEARCH_PREFIX}/v1/web")
        e["WEB_SEARCH_MOUNT_PREFIX"] = SEARCH_PREFIX
        e.setdefault("WEB_SEARCH_NETWORK", hosted_settings.x402_network)
        e.setdefault("WEB_SEARCH_X402", "0")
        e.setdefault(
            "WEB_SEARCH_DATA_DIR", str(Path(hosted_settings.data_dir).parent / "web-search")
        )
        settings = mod.Settings.from_env(e, prefix="WEB_SEARCH_")
        if contract.atomic(settings.price_usd) <= credit:
            raise RuntimeError("WEB_SEARCH_PRICE_USD must exceed the supplier's cost per search")
        if settings.x402:
            taken = {
                a.lower()
                for a in (hosted_settings.x402_pay_to, e.get("READ_PAY_TO"), e.get("SEARCH_PAY_TO"))
                if a
            }
            if settings.pay_to.lower() in taken:
                raise RuntimeError("WEB_SEARCH_X402=1 needs its own WEB_SEARCH_PAY_TO")
        supplier = suppliers.SerpexSearch(e["WEB_SEARCH_SUPPLIER_KEY"], credit)
        task = search.search_task(mod, supplier, _search_retry_secret(e))
        return mod.create_app(settings, task=task), settings
    except Exception:
        log.exception("Web Search not mounted: configuration error")
        return None


def finder_from_env(hosted_settings: Any, env: dict[str, str] | None = None) -> Any:
    """(app, settings) for Inferrail x402 Finder when FINDER_PAY_TO is set, else None.

    x402 (FINDER_X402=1) pays FINDER_X402_PAY_TO, an address no other x402 service uses; MPP keeps
    paying FINDER_PAY_TO. No supplier cost (the catalog is the CDP Bazaar's free discovery API).
    Mounted only with
    FINDER_CATALOG_RIGHTS_CONFIRMED=1: the operator's confirmation that the CDP terms permit
    serving answers built from that data. MPP only unless
    FINDER_X402=1, which needs a receiving address of its own, like Web Search. Fail-isolated:
    a configuration error leaves it unmounted.
    """
    e = dict(os.environ if env is None else env)
    if not e.get("FINDER_PAY_TO"):
        return None
    try:
        # The catalog is CDP Bazaar data; serving answers built from it needs the operator's
        # confirmation that the CDP terms allow this use (like Web Search's supplier rights).
        if e.get("FINDER_CATALOG_RIGHTS_CONFIRMED") != "1":
            raise RuntimeError("x402 Finder needs FINDER_CATALOG_RIGHTS_CONFIRMED=1")
        mod = sys.modules.get("web_read_service") or _load(
            "web_read_service", HOSTED_DIR.parent / "web_read" / "service.py"
        )
        finder = _load("web_read_finder", HOSTED_DIR.parent / "web_read" / "finder.py")
        origin = hosted_settings.public_base_url.removesuffix(PREFIX)
        e.setdefault("FINDER_RESOURCE_URL", f"{origin}{FIND_PREFIX}/v1/find")
        e["FINDER_MOUNT_PREFIX"] = FIND_PREFIX
        e.setdefault("FINDER_NETWORK", hosted_settings.x402_network)
        e.setdefault("FINDER_X402", "0")
        e.setdefault("FINDER_DATA_DIR", str(Path(hosted_settings.data_dir).parent / "finder"))
        settings = mod.Settings.from_env(e, prefix="FINDER_")
        if settings.x402:
            taken = {
                a.lower()
                for a in (hosted_settings.x402_pay_to, e.get("READ_PAY_TO"),
                          e.get("SEARCH_PAY_TO"), e.get("WEB_SEARCH_PAY_TO"),
                          e.get("PKG_X402_PAY_TO"), e.get("DOMAIN_X402_PAY_TO"))
                if a
            }  # fmt: skip
            if settings.x402_recipient.lower() in taken:
                raise RuntimeError("FINDER_X402=1 needs its own FINDER_X402_PAY_TO")
        catalog = finder.Catalog()
        task = finder.finder_task(mod, catalog, _finder_retry_secret(e))
        app = mod.create_app(settings, task=task)
        app.state.finder_catalog = catalog

        @app.middleware("http")
        async def warm_catalog(request: Any, call_next: Any) -> Any:
            catalog.ensure_fresh()  # any request (even an unpaid quote) starts the first load
            return await call_next(request)

        @app.get("/v1/catalog", include_in_schema=False)
        async def catalog_status() -> dict[str, Any]:
            """Free: whether the catalog is loaded and how fresh it is (no picks, no charge)."""
            as_of = catalog.as_of
            return {"loaded": as_of is not None, "entries": len(catalog.entries),
                    "as_of": None if as_of is None else time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(as_of))}  # fmt: skip

        return app, settings
    except Exception:
        log.exception("x402 Finder not mounted: configuration error")
        return None


def package_check_from_env(hosted_settings: Any, env: dict[str, str] | None = None) -> Any:
    """(app, settings) for Inferrail Package Check when PKG_PAY_TO is set, else None.

    Data is OSV.dev's free public API (no key); no supplier cost. MPP only unless PKG_X402=1,
    which pays PKG_X402_PAY_TO, an address no other x402 service uses (an EIP-3009 transfer binds
    a merchant, not a route); MPP keeps paying PKG_PAY_TO. Fail-isolated: a configuration error
    leaves it unmounted.
    """
    e = dict(os.environ if env is None else env)
    if not e.get("PKG_PAY_TO"):
        return None
    try:
        mod = sys.modules.get("web_read_service") or _load(
            "web_read_service", HOSTED_DIR.parent / "web_read" / "service.py"
        )
        packages = _load("web_read_packages", HOSTED_DIR.parent / "web_read" / "packages.py")
        origin = hosted_settings.public_base_url.removesuffix(PREFIX)
        e.setdefault("PKG_RESOURCE_URL", f"{origin}{PKG_PREFIX}/v1/check")
        e["PKG_MOUNT_PREFIX"] = PKG_PREFIX
        e.setdefault("PKG_NETWORK", hosted_settings.x402_network)
        e.setdefault("PKG_X402", "0")
        e.setdefault("PKG_DATA_DIR", str(Path(hosted_settings.data_dir).parent / "packages"))
        settings = mod.Settings.from_env(e, prefix="PKG_")
        if settings.x402:
            taken = {
                a.lower()
                for a in (hosted_settings.x402_pay_to, e.get("READ_PAY_TO"),
                          e.get("SEARCH_PAY_TO"), e.get("WEB_SEARCH_PAY_TO"),
                          e.get("FINDER_X402_PAY_TO"), e.get("DOMAIN_X402_PAY_TO"))
                if a
            }  # fmt: skip
            if settings.x402_recipient.lower() in taken:
                raise RuntimeError("PKG_X402=1 needs its own PKG_X402_PAY_TO")
        task = packages.package_task(mod, _package_retry_secret(e))
        return mod.create_app(settings, task=task), settings
    except Exception:
        log.exception("Package Check not mounted: configuration error")
        return None


def _package_retry_secret(e: dict[str, str]) -> bytes:
    """Keys the retry identity (an HMAC of the package list). Stable across restarts."""
    if e.get("PKG_RETRY_SECRET"):
        return e["PKG_RETRY_SECRET"].encode()
    seed = e.get("CDP_API_KEY_SECRET") or e.get("BG_OPERATOR_TOKEN")
    if not seed:
        raise RuntimeError("Package Check needs PKG_RETRY_SECRET or BG_OPERATOR_TOKEN")
    return hashlib.sha256(f"inferrail-package-check-retry|{seed}".encode()).digest()


def domain_check_from_env(hosted_settings: Any, env: dict[str, str] | None = None) -> Any:
    """(app, settings) for Inferrail Domain Check when DOMAIN_PAY_TO is set, else None.

    Data is Cloudflare's public DNS-over-HTTPS resolver and registry RDAP services (no key, no
    supplier cost). MPP only unless DOMAIN_X402=1, which pays DOMAIN_X402_PAY_TO, an address no
    other x402 service uses (an EIP-3009 transfer binds a merchant, not a route); MPP keeps paying
    DOMAIN_PAY_TO. Fail-isolated: a configuration error leaves it unmounted.
    """
    e = dict(os.environ if env is None else env)
    if not e.get("DOMAIN_PAY_TO"):
        return None
    try:
        mod = sys.modules.get("web_read_service") or _load(
            "web_read_service", HOSTED_DIR.parent / "web_read" / "service.py"
        )
        domains = _load("web_read_domains", HOSTED_DIR.parent / "web_read" / "domains.py")
        origin = hosted_settings.public_base_url.removesuffix(PREFIX)
        e.setdefault("DOMAIN_RESOURCE_URL", f"{origin}{DOMAIN_PREFIX}/v1/lookup")
        e["DOMAIN_MOUNT_PREFIX"] = DOMAIN_PREFIX
        e.setdefault("DOMAIN_NETWORK", hosted_settings.x402_network)
        e.setdefault("DOMAIN_X402", "0")
        e.setdefault("DOMAIN_DATA_DIR", str(Path(hosted_settings.data_dir).parent / "domains"))
        settings = mod.Settings.from_env(e, prefix="DOMAIN_")
        if settings.x402:
            taken = {
                a.lower()
                for a in (hosted_settings.x402_pay_to, e.get("READ_PAY_TO"),
                          e.get("SEARCH_PAY_TO"), e.get("WEB_SEARCH_PAY_TO"),
                          e.get("FINDER_X402_PAY_TO"), e.get("PKG_X402_PAY_TO"))
                if a
            }  # fmt: skip
            if settings.x402_recipient.lower() in taken:
                raise RuntimeError("DOMAIN_X402=1 needs its own DOMAIN_X402_PAY_TO")
        task = domains.domain_task(mod, _domain_retry_secret(e))
        return mod.create_app(settings, task=task), settings
    except Exception:
        log.exception("Domain Check not mounted: configuration error")
        return None


def _domain_retry_secret(e: dict[str, str]) -> bytes:
    """Keys the retry identity (an HMAC of the request). Stable across restarts."""
    if e.get("DOMAIN_RETRY_SECRET"):
        return e["DOMAIN_RETRY_SECRET"].encode()
    seed = e.get("CDP_API_KEY_SECRET") or e.get("BG_OPERATOR_TOKEN")
    if not seed:
        raise RuntimeError("Domain Check needs DOMAIN_RETRY_SECRET or BG_OPERATOR_TOKEN")
    return hashlib.sha256(f"inferrail-domain-check-retry|{seed}".encode()).digest()


def _finder_retry_secret(e: dict[str, str]) -> bytes:
    """Keys the retry identity (an HMAC of the request). Stable across restarts."""
    if e.get("FINDER_RETRY_SECRET"):
        return e["FINDER_RETRY_SECRET"].encode()
    seed = e.get("CDP_API_KEY_SECRET") or e.get("BG_OPERATOR_TOKEN")
    if not seed:
        raise RuntimeError("x402 Finder needs FINDER_RETRY_SECRET or BG_OPERATOR_TOKEN")
    return hashlib.sha256(f"inferrail-x402-finder-retry|{seed}".encode()).digest()


def _search_retry_secret(e: dict[str, str]) -> bytes:
    """Keys the retry identity (an HMAC of the query). Stable across restarts, so a paid result
    can still be recovered after a deploy."""
    if e.get("WEB_SEARCH_RETRY_SECRET"):
        return e["WEB_SEARCH_RETRY_SECRET"].encode()
    seed = e.get("CDP_API_KEY_SECRET") or e.get("BG_OPERATOR_TOKEN")
    if not seed:
        raise RuntimeError("Web Search needs WEB_SEARCH_RETRY_SECRET or BG_OPERATOR_TOKEN")
    return hashlib.sha256(f"inferrail-web-search-retry|{seed}".encode()).digest()


def _mpp_protocols(settings: Any) -> list[dict[str, Any]]:
    """The MPP (Tempo) offer, when Web Read also accepts it."""
    if not getattr(settings, "mpp", None):
        return []
    from mpp.methods.tempo import PATH_USD, USDC

    currency = USDC if settings.mpp.chain_id == 4217 else PATH_USD
    return [{"mpp": {"method": "tempo", "intent": "charge", "currency": currency}}]


# Catalogs that route agents' tasks match a task against each operation's summary,
# description and tags; a long summary + description can end up as one long "alias" that no
# short task phrase matches. Keep the summary a task phrase, lead the description with what it
# does, and tag the tasks.
# Agents with budgets ask catalogs to "scrape a website"; the paid listings they find are phrased
# that way, so the summary leads with it (Web Read does fetch and extract the page).
WEB_READ_SUMMARY = "Scrape a web page to clean markdown: any public URL, with its metadata"
WEB_READ_TAGS = ["web scraping", "scrape", "web page to markdown", "url to markdown", "markdown"]


def _web_read_description(price_usd: str) -> str:
    return (
        "Scrape and read any public web page as clean, LLM-ready markdown (headings, lists, links, "
        "tables) with its metadata (description, canonical URL, authors, published date, "
        f"language). URL to markdown for articles, docs and product pages. ${price_usd} per "
        "delivered page; a missing, blocked or refused page, a bot check or a JavaScript-only "
        "page costs nothing, and a retry never pays twice."
    )


def _web_read_batch_paths(settings: Any) -> dict[str, Any]:
    batch = sys.modules["web_read_batch"]
    top = sys.modules["web_read_service"].price_for(settings.price_usd, batch.MAX_URLS)
    return {
        f"{READ_PREFIX}/v1/pages": {
            "post": {
                "operationId": "readPages",
                "summary": batch.SUMMARY,
                "description": batch.DESCRIPTION,
                "tags": WEB_READ_TAGS,
                "security": [],
                "x-payment-info": {
                    # N URLs cost N x the page price: the 402 for a request quotes its total.
                    "price": {"mode": "dynamic", "currency": "USD", "min": settings.price_usd,
                              "max": top},
                    "protocols": [{"x402": {}}] + _mpp_protocols(settings),
                },
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": batch.PagesRequest.model_json_schema(),
                            "example": {"urls": ["https://example.com/",
                                                 "https://example.org/"]},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Every page, delivered and paid",
                        "content": {"application/json": {"schema": batch.output_schema(
                            sys.modules["web_read_service"].OUTPUT_SCHEMA)}},
                    },
                    "402": {"description": "Payment Required (quotes N x the page price)"},
                    "422": {"description": "Invalid request, or a page can't be read "
                            "(results lists which); nothing charged"},
                    "502": {"description": "A page's site failed; may succeed later; nothing "
                            "charged"},
                    "504": {"description": "The batch timed out; nothing charged"},
                },
            }
        }
    }  # fmt: skip


def _web_read_paths(settings: Any) -> dict[str, Any]:
    mod = sys.modules["web_read_service"]
    shared = {
        "summary": WEB_READ_SUMMARY,
        "description": _web_read_description(settings.price_usd),
        "tags": WEB_READ_TAGS,
        "security": [],
        "x-payment-info": {
            "price": {"mode": "fixed", "currency": "USD", "amount": settings.price_usd},
            "protocols": [{"x402": {}}] + _mpp_protocols(settings),
        },
    }
    responses = {
        "200": {
            "description": "The page, delivered and paid",
            "content": {"application/json": {"schema": mod.OUTPUT_SCHEMA}},
        },
        "402": {"description": "Payment Required"},
        "422": {
            "description": "Not readable (see error), including a target-site 4xx "
            "(missing, gone, forbidden); retrying the same URL won't help; nothing "
            "charged"
        },
        "502": {
            "description": "Target site failed (5xx, 408, 429, connection); may "
            "succeed later; nothing charged"
        },
        "504": {"description": "Target site timed out; nothing charged"},
    }
    operations: dict[str, Any] = {
        "post": {
            "operationId": "readPage",
            **shared,
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": mod.PageRequest.model_json_schema(),
                        # Catalogs build their first call from this (Mercator's
                        # inputExample); a real, stable page.
                        "example": {"url": "https://example.com/"},
                    }
                },
            },
            "responses": responses,
        }
    }
    if getattr(settings, "allow_get", False):
        # The same sale over GET (the URL in the query string), already served when enabled;
        # many agent clients and routers call page readers this way.
        fields = mod.PageRequest.model_json_schema()["properties"]
        operations["get"] = {
            "operationId": "readPageGet",
            **shared,
            "parameters": [
                {
                    "name": "url",
                    "in": "query",
                    "required": True,
                    "schema": fields["url"],
                    "example": "https://example.com/",
                },
                {
                    "name": "request_id",
                    "in": "query",
                    "required": False,
                    "schema": fields["request_id"],
                },
            ],
            "responses": responses,
        }
    return {f"{READ_PREFIX}/v1/page": operations}


def _web_search_paths(settings: Any) -> dict[str, Any]:
    search = sys.modules["web_read_search"]
    protocols = ([{"x402": {}}] if settings.x402 else []) + _mpp_protocols(settings)
    return {
        f"{SEARCH_PREFIX}/v1/web": {
            "post": {
                "operationId": "searchWeb",
                "summary": search.SUMMARY,
                "description": (
                    "Search the web for agents: ranked title, URL and snippet results for a "
                    f"query, with a content hash and a receipt. ${settings.price_usd} per "
                    "delivered search; a failed or empty search costs nothing, and a retry with "
                    "the same request_id never pays twice. The query is not stored."
                ),
                "tags": search.TAGS,
                "security": [],
                "x-payment-info": {
                    "price": {"mode": "fixed", "currency": "USD", "amount": settings.price_usd},
                    "protocols": protocols,
                },
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": search.SearchRequest.model_json_schema(),
                            "example": {"query": "tempo machine payments protocol"},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "The results, delivered and paid",
                        "content": {"application/json": {"schema": search.OUTPUT_SCHEMA}},
                    },
                    "402": {"description": "Payment Required"},
                    "422": {"description": "Invalid request; nothing charged"},
                    "502": {"description": "The search failed; may succeed later; nothing charged"},
                },
            }
        }
    }


def _finder_paths(settings: Any) -> dict[str, Any]:
    finder = sys.modules["web_read_finder"]
    protocols = ([{"x402": {}}] if settings.x402 else []) + _mpp_protocols(settings)
    return {
        f"{FIND_PREFIX}/v1/find": {
            "post": {
                "operationId": "findApi",
                "summary": finder.SUMMARY,
                "description": finder.DESCRIPTION.replace("Pay only", f"${settings.price_usd} "
                                                          "per delivered answer. Pay only"),
                "tags": finder.TAGS,
                "security": [],
                "x-payment-info": {
                    "price": {"mode": "fixed", "currency": "USD", "amount": settings.price_usd},
                    "protocols": protocols,
                },
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": finder.FindRequest.model_json_schema(),
                            "example": {"need": "read a web page as markdown"},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "The picks, delivered and paid",
                        "content": {"application/json": {"schema": finder.OUTPUT_SCHEMA}},
                    },
                    "402": {"description": "Payment Required"},
                    "422": {"description": "No match or invalid request; nothing charged"},
                    "503": {"description": "Catalog loading; retry shortly; nothing charged"},
                },
            }
        }
    }  # fmt: skip


def _package_paths(settings: Any) -> dict[str, Any]:
    packages = sys.modules["web_read_packages"]
    protocols = ([{"x402": {}}] if settings.x402 else []) + _mpp_protocols(settings)
    return {
        f"{PKG_PREFIX}/v1/check": {
            "post": {
                "operationId": "checkPackages",
                "summary": packages.SUMMARY,
                "description": packages.DESCRIPTION.replace("Pay only", f"${settings.price_usd} "
                                                            "per delivered check. Pay only"),
                "tags": packages.TAGS,
                "security": [],
                "x-payment-info": {
                    "price": {"mode": "fixed", "currency": "USD", "amount": settings.price_usd},
                    "protocols": protocols,
                },
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": packages.CheckRequest.model_json_schema(),
                            "example": {"packages": [{"ecosystem": "npm", "name": "lodash",
                                                      "version": "4.17.15"}]},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "The check, delivered and paid",
                        "content": {"application/json": {"schema": packages.OUTPUT_SCHEMA}},
                    },
                    "402": {"description": "Payment Required"},
                    "422": {"description": "Invalid request; nothing charged"},
                    "502": {
                        "description": "OSV.dev unreachable; may succeed later; nothing charged"
                    },
                },
            }
        }
    }  # fmt: skip


def _domain_paths(settings: Any) -> dict[str, Any]:
    domains = sys.modules["web_read_domains"]
    protocols = ([{"x402": {}}] if settings.x402 else []) + _mpp_protocols(settings)
    return {
        f"{DOMAIN_PREFIX}/v1/lookup": {
            "post": {
                "operationId": "lookupDomain",
                "summary": domains.SUMMARY,
                "description": domains.DESCRIPTION.replace("Pay only", f"${settings.price_usd} "
                                                           "per delivered lookup. Pay only"),
                "tags": domains.TAGS,
                "security": [],
                "x-payment-info": {
                    "price": {"mode": "fixed", "currency": "USD", "amount": settings.price_usd},
                    "protocols": protocols,
                },
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": domains.LookupRequest.model_json_schema(),
                            "example": {"domain": "example.com"},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "The lookup, delivered and paid",
                        "content": {"application/json": {"schema": domains.OUTPUT_SCHEMA}},
                    },
                    "402": {"description": "Payment Required"},
                    "422": {"description": "Invalid or reserved name; nothing charged"},
                    "502": {
                        "description": "DNS resolver unreachable; may succeed later; nothing "
                        "charged"
                    },
                },
            }
        }
    }  # fmt: skip


def _search_rails(settings: Any) -> str:
    return " or ".join(
        r for r, on in (("x402 on Base", settings.x402), ("MPP on Tempo", settings.mpp)) if on
    )


def _origin_summary(app: Starlette) -> tuple[str, str]:
    """(title, one-paragraph description) of what this origin sells, for catalogs."""
    settings = app.state.hosted_settings
    hosted = (
        "Inferrail Hosted: one dollar budget shared by every agent and process using a "
        f"workspace key ({settings.free_runs_per_month} governed runs a month free, then "
        "$0.001/run in x402 credits)."
    )
    read = getattr(app.state, "web_read_settings", None)
    found = getattr(app.state, "web_search_settings", None)
    finder = getattr(app.state, "finder_settings", None)
    pkg = getattr(app.state, "package_settings", None)
    dom = getattr(app.state, "domain_settings", None)
    if not app.state.discovery_paths or (read is None and found is None and finder is None
                                         and pkg is None and dom is None):  # fmt: skip
        return "Inferrail Hosted", hosted
    # Agent clients copy this paragraph verbatim as the tool description a model reads to decide
    # whether to use the origin (AgentCash's MCP instructions and its installed SKILL.md), so it
    # leads with the tasks and prices; Hosted, which needs your own provider key, comes last.
    parts = ["Pay-per-call tools for agents, charged only on delivery."]
    if read is not None:
        parts.append(
            "Inferrail Web Read: any public web page as clean markdown (URL to markdown), "
            f"${read.price_usd} per delivered page, paid over {_rails(read)}; failed pages are "
            "free and a retry never pays twice."
        )
    if found is not None:
        parts.append(
            "Inferrail Web Search: ranked web search results for a query for "
            f"${found.price_usd} per delivered search, paid over {_search_rails(found)}; failed "
            "searches are never charged."
        )
    if finder is not None:
        parts.append(
            "Inferrail x402 Finder: which paid x402 API fits a task, ranked by real 30-day paid "
            f"usage, ${finder.price_usd} per answer, paid over {_search_rails(finder)}; no match "
            "is free."
        )
    if pkg is not None:
        parts.append(
            "Inferrail Package Check: known vulnerabilities (CVE, severity, fixed versions) for up "
            f"to 20 package versions, ${pkg.price_usd} per check, paid over {_search_rails(pkg)}."
        )
    if dom is not None:
        parts.append(
            "Inferrail Domain Check: a domain's DNS records, SPF/DMARC and registration "
            f"(registrar, created and expiry dates, status) from RDAP, ${dom.price_usd} per "
            f"lookup, paid over {_search_rails(dom)}."
        )
    # Catalog search often matches only an origin's title and URL (x402scan / AgentCash index a v2
    # endpoint's description nowhere searchable), so the title names the main task, not just us.
    title = "Inferrail"
    if read is not None:
        title = "Inferrail: web page to markdown and checks for agents"
    return title, " ".join(parts) + " " + hosted


def _rails(read: Any) -> str:
    """The payment rails Web Read accepts, as an agent would look for them."""
    return "x402 on Base and MPP on Tempo" if read.mpp else "x402 on Base"


def origin_api_catalog(app: Starlette) -> dict[str, Any]:
    """`/.well-known/api-catalog` (RFC 9727): a linkset pointing API catalogs (Mercator reads
    it) at this origin's OpenAPI description."""
    origin = app.state.hosted_settings.public_base_url.removesuffix(PREFIX)
    return {
        "linkset": [
            {
                "anchor": origin,
                "service-desc": [
                    {"href": f"{origin}/openapi.json", "type": "application/vnd.oai.openapi+json"}
                ],
            }
        ]
    }


def origin_llms(app: Starlette) -> str:
    """`/llms.txt` at the origin root: where an agent that only knows this origin (from a
    registry listing or a 402) starts. An index of each service's own guide and the contract;
    the facts come from the same settings the services run with."""
    settings = app.state.hosted_settings
    base = settings.public_base_url
    origin = base.removesuffix(PREFIX)
    title, summary = _origin_summary(app)
    lines = [f"# {title}", "", f"> {summary}", "", "## Services", ""]
    read = getattr(app.state, "web_read_settings", None)
    if app.state.discovery_paths and read is not None:
        lines += [
            f"- [Inferrail Web Read]({origin}{READ_PREFIX}/llms.txt): "
            f'`POST {origin}{READ_PREFIX}/v1/page` with `{{"url": "https://...", '
            f'"request_id": "..."}}` returns HTTP 402; pay ${read.price_usd} over '
            f"{_rails(read).replace(' and ', ' or ')} and repeat the request."
            + (
                f" Also an MCP tool: `read_page` at `{origin}{READ_PREFIX}/mcp/` "
                "(streamable HTTP, paid with x402 in the tool call's `_meta`)."
                if getattr(read, "mcp", False)
                else ""
            ),
        ]
    read_batch = getattr(app.state, "web_read_batch_settings", None)
    if app.state.discovery_paths and read_batch is not None:
        lines += [
            f'- Several pages at once: `POST {origin}{READ_PREFIX}/v1/pages` with `{{"urls": '
            '["https://...", ...], "request_id": "..."}` (1-10 URLs) returns HTTP 402 for N x '
            f"${read_batch.price_usd}; it is charged only if every page is delivered, and a "
            "failure lists which URLs to drop.",
        ]
    found = getattr(app.state, "web_search_settings", None)
    if app.state.discovery_paths and found is not None:
        lines += [
            f"- [Inferrail Web Search]({origin}{SEARCH_PREFIX}/llms.txt): "
            f'`POST {origin}{SEARCH_PREFIX}/v1/web` with `{{"query": "...", '
            f'"request_id": "..."}}` returns HTTP 402; pay ${found.price_usd} over '
            f"{_search_rails(found)} and repeat the request.",
        ]
    finder = getattr(app.state, "finder_settings", None)
    if app.state.discovery_paths and finder is not None:
        lines += [
            f"- [Inferrail x402 Finder]({origin}{FIND_PREFIX}/llms.txt): "
            f'`POST {origin}{FIND_PREFIX}/v1/find` with `{{"need": "...", '
            f'"request_id": "..."}}` returns HTTP 402; pay ${finder.price_usd} over '
            f"{_search_rails(finder)} and repeat the request. Up to five paid x402 APIs for "
            "the task, ranked by real 30-day paid usage.",
        ]
    pkg = getattr(app.state, "package_settings", None)
    if app.state.discovery_paths and pkg is not None:
        lines += [
            f"- [Inferrail Package Check]({origin}{PKG_PREFIX}/llms.txt): "
            f'`POST {origin}{PKG_PREFIX}/v1/check` with `{{"packages": [{{"ecosystem": "npm", '
            '"name": "...", "version": "..."}]}` returns HTTP 402; pay '
            f"${pkg.price_usd} over {_search_rails(pkg)} and repeat the request. Known "
            "vulnerabilities for up to 20 package versions, from OSV.dev.",
        ]
    dom = getattr(app.state, "domain_settings", None)
    if app.state.discovery_paths and dom is not None:
        lines += [
            f"- [Inferrail Domain Check]({origin}{DOMAIN_PREFIX}/llms.txt): "
            f'`POST {origin}{DOMAIN_PREFIX}/v1/lookup` with `{{"domain": "example.com"}}` returns '
            f"HTTP 402; pay ${dom.price_usd} over {_search_rails(dom)} and repeat the request. "
            "DNS records, SPF/DMARC and RDAP registration for one domain.",
        ]
    lines += [
        f"- [Inferrail Hosted]({base}/llms.txt): per-run dollar budgets shared across agents; "
        f"{settings.free_runs_per_month} governed runs a month free.",
        "",
    ]
    if app.state.discovery_paths and any(x is not None for x in (read, finder, pkg, dom)):
        lines += [
            "## Use from an agent wallet you already have",
            "",
            f"- AgentCash: `npx agentcash add {origin}` lists these services in your agent's paid "
            "tools (its MCP server and an installed skill); it pays from the AgentCash wallet.",
            "- Coinbase agentic wallet: `npx awal x402 pay "
            f'{origin}{READ_PREFIX}/v1/page -X POST -d \'{{"url": "https://..."}}\'`.',
            "- Vercel AI SDK: `npm i @inferrail/ai-sdk-tools ai zod`, then "
            "`tools: inferrailTools({ fetch })` with your x402-paying fetch (for example "
            "`wrapFetchWithPayment` from @x402/fetch). The package holds no wallet or funds; "
            "it pays only through the fetch you pass.",
            "- MCP client (Claude Code, Claude Desktop, Cursor): run "
            "`npx -y @inferrail/ai-sdk-tools` as a local MCP server with `INFERRAIL_WALLET_KEY` "
            "(a wallet you fund with a little USDC on Base) and `INFERRAIL_MAX_USD_PER_DAY` "
            "(default 0.10). It refuses to sign past the daily budget or above a call's price.",
        ]
        if getattr(app.state, "read_credits", False):
            lines += [
                f"- API key, no wallet (prepaid credits): a person opens {origin}{PREFIX}/v1/start "
                "in a browser to create a key and pay by card once; or get a free key with `POST "
                f"{origin}{PREFIX}/v1/workspaces`; a person funds it once at `POST "
                f"{origin}{PREFIX}/v1/credits/checkout` (card, $10 = 10,000 credits) or "
                f"`POST {origin}{PREFIX}/v1/credits/x402` (USDC). Then "
                f"`POST {origin}{READ_PREFIX}/credits/v1/page` with "
                "`Authorization: Bearer irw_...` reads a page for 2 credits ($0.002), charged "
                "only when delivered. The MCP server takes the key as `INFERRAIL_API_KEY`; the "
                "AI SDK as `inferrailCreditTools({ apiKey })`.",
            ]
        lines += [""]
    lines += [
        "## Contract",
        "",
        f"- [OpenAPI]({origin}/openapi.json): every operation here, with price and schemas.",
        f"- [Source and docs]({REPO})",
        "",
    ]
    return "\n".join(lines)


def origin_home(app: Starlette) -> str:
    """The origin root: a minimal page with the title, description and icon that catalogs read
    for the origin's listing (the APIs themselves are described in /openapi.json)."""
    import html

    title, description = (html.escape(t) for t in _origin_summary(app))
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{title}</title>"
        f'<meta name="description" content="{description}">'
        f'<meta property="og:title" content="{title}">'
        f'<meta property="og:description" content="{description}">'
        f'<meta property="og:image" content="{SITE}/favicon-192x192.png">'
        f'<link rel="icon" href="{SITE}/favicon.ico">'
        f"</head><body><h1>{title}</h1><p>{description}</p>"
        '<p>Machine-readable: <a href="/llms.txt">/llms.txt</a>, '
        '<a href="/openapi.json">/openapi.json</a>. Docs: '
        f'<a href="{REPO}">{REPO}</a>.</p></body></html>'
    )


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
    title, summary = _origin_summary(app)
    read_intro = summary.split(" Inferrail Hosted:")[0] + " " if title != "Inferrail Hosted" else ""
    origin = base.removesuffix(PREFIX)
    guidance = (
        f"Hosted: create a workspace (POST {base}/v1/workspaces), then call "
        f"{base}/v1/chat/completions or {base}/v1/messages with the workspace key, your own "
        "provider key, a work id and a ceiling; full guide at " + base + "/llms.txt."
    )
    read = getattr(app.state, "web_read_settings", None)
    if app.state.discovery_paths and read is not None:
        guidance = (
            f'Web Read: POST {origin}{READ_PREFIX}/v1/page with {{"url": ...}}; pay the '
            f"{'x402 or MPP' if read.mpp else 'x402'} challenge; HTTP 200 "
            "is the page. Send a request_id and retry with it if the "
            f"response is lost (no second charge). Guide: {origin}{READ_PREFIX}/llms.txt. "
            + (
                f"Or as the MCP tool read_page at {origin}{READ_PREFIX}/mcp/ (x402 in _meta). "
                if getattr(read, "mcp", False)
                else ""
            )
        ) + guidance
    finder = getattr(app.state, "finder_settings", None)
    if app.state.discovery_paths and finder is not None:
        guidance = (
            f'Finder: POST {origin}{FIND_PREFIX}/v1/find with {{"need": "..."}} for up to five '
            "paid x402 APIs that fit a task, ranked by 30-day paid usage; pay the "
            f"{_search_rails(finder)} challenge; "
            f"no match is not charged. Guide: {origin}{FIND_PREFIX}/llms.txt. "
        ) + guidance
    pkg = getattr(app.state, "package_settings", None)
    if app.state.discovery_paths and pkg is not None:
        guidance = (
            f'Package Check: POST {origin}{PKG_PREFIX}/v1/check with {{"packages": [{{"ecosystem": '
            '"npm", "name": "...", "version": "..."}]} (1-20) before installing or shipping '
            f"dependencies; pay the {_search_rails(pkg)} challenge; an OSV.dev outage is not "
            f"charged. Guide: {origin}{PKG_PREFIX}/llms.txt. "
        ) + guidance
    dom = getattr(app.state, "domain_settings", None)
    if app.state.discovery_paths and dom is not None:
        guidance = (
            f'Domain Check: POST {origin}{DOMAIN_PREFIX}/v1/lookup with {{"domain": "..."}} for '
            "DNS records, SPF/DMARC and RDAP registration; pay the "
            f"{_search_rails(dom)} challenge; a resolver outage is not charged. Guide: "
            f"{origin}{DOMAIN_PREFIX}/llms.txt. "
        ) + guidance
    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": "1",
            "description": read_intro
            + (
                "Also Inferrail Hosted: a dollar budget shared across agents' OpenAI and "
                "Anthropic calls (your own provider key). How to call each: x-guidance."
                if read_intro
                else "Inferrail Hosted: one dollar budget shared by every agent and process that "
                "uses a workspace key, reserved before each OpenAI or Anthropic call, so "
                "parallel agents can't "
                f"overspend a run. Create a workspace free with POST {base}/v1/workspaces; "
                f"{settings.free_runs_per_month} governed runs a month are free. Send calls "
                f"to {base}/v1/chat/completions (OpenAI shape) or {base}/v1/messages "
                "(Anthropic shape) with your own provider key, a work id and a ceiling. "
                f"More runs: USDC over x402 at POST {base}/v1/credits/x402."
            ),
            "x-guidance": guidance,
            "contact": {"name": "Inferrail", "url": REPO},
        },
        "servers": [{"url": origin}],
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
    read_op = (doc["paths"].get(f"{READ_PREFIX}/v1/page") or {}).get("post")
    if getattr(app.state, "read_credits", False) and read_op:
        # The same read, paid from prepaid workspace credits with an API key instead of x402.
        op = {k: v for k, v in read_op.items() if not k.startswith("x-payment")}
        op.update(
            operationId="webReadWithCredits",
            summary="Scrape a web page to clean markdown, paid from prepaid credits (API key)",
            description=(
                "The same read as POST /read/v1/page, paid from a workspace's prepaid credits: 2 "
                "credits ($0.002) per delivered page, nothing for a page that can't be read. A "
                f"person funds the workspace once (POST {base}/v1/credits/checkout, card), then "
                "calls with 'Authorization: Bearer irw_...' are paid from the balance. 402 "
                "insufficient_credits when the balance is too low."
            ),
            security=[{"WorkspaceKey": []}],
        )
        doc["paths"][f"{READ_PREFIX}/credits/v1/page"] = {"post": op}
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
