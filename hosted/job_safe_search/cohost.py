"""Serve Job-Safe Search alongside the existing trial and Hosted applications.

Search remains a separate ASGI application with its own ledger, writer lease,
capability secret and payment gates. Selecting this entrypoint changes no live
service or financial approval by itself. Use exactly one worker.
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.routing import Mount

from hosted.budget_gateway import cohost as hosted_cohost

PREFIX = "/job-search"


def create_cohosted_app(
    trial_data_dir: Path,
    hosted_settings: Any,
    search_app: Starlette,
    *,
    facilitator: Any | None = None,
    modules: Any | None = None,
) -> Starlette:
    """Reuse a paid instance while keeping each application's financial state separate."""
    search = search_app.state.search
    resource = urlsplit(search.config.resource_url)
    if resource.path != PREFIX + "/search" or resource.query or resource.fragment:
        raise ValueError("cohosted_search_resource_requires_job_search_prefix")
    search_dir = search.store.path.resolve().parent
    hosted_dir = hosted_settings.data_dir.resolve()
    directories = (search_dir, hosted_dir, trial_data_dir.resolve())
    if any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(directories)
        for right in directories[index + 1 :]
    ):
        raise ValueError("cohosted_search_requires_separate_data_directory")
    if (
        hosted_settings.x402_pay_to
        and hosted_settings.x402_network == search.config.network
        and hosted_settings.x402_pay_to.lower() == search.config.pay_to.lower()
    ):
        # EIP-3009 binds transfers to a merchant/amount, not an endpoint. Distinct
        # merchants prevent one proxy-settled transfer satisfying two ledgers.
        raise ValueError("cohosted_payment_merchants_must_be_distinct")
    combined = hosted_cohost.create_combined_app(
        trial_data_dir, hosted_settings, facilitator=facilitator, modules=modules
    )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(combined.router.lifespan_context(combined))
            await stack.enter_async_context(search_app.router.lifespan_context(search_app))
            yield

    app = Starlette(
        routes=[Mount(PREFIX, app=search_app), Mount("/", app=combined)], lifespan=lifespan
    )
    app.state.search_app = search_app
    app.state.combined_app = combined
    return app


def production_app() -> Starlette:
    from .service import production_app as search_factory

    modules = hosted_cohost.load_modules()
    return create_cohosted_app(
        Path(os.environ.get("COST_GATEWAY_DATA_DIR", "/tmp/inferrail_cost_gateway")),
        hosted_cohost.settings_from_env(modules[1]),
        search_factory(),
        modules=modules,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "hosted.job_safe_search.cohost:production_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8423")),
        log_level="warning",
        workers=1,
    )
