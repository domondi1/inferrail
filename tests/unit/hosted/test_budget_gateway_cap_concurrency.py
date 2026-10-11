"""Concurrent purchases can't slip past the sales caps.

The purchase handler checks both caps and records the pending purchase with no `await` between
them, inside the single service process. So concurrent buyers see each other's pending
purchases, and the caps hold under a burst. Here 30 buyers pay at once against the launch
limits ($1 packs, $25 per 24 hours, $50 outstanding): exactly 25 settle, the rest get 503 and are
never settled (not charged).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

pytest.importorskip("x402")
pytest.importorskip("eth_account")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import (  # noqa: E402
    FakeFacilitator,
    decode_payment_required,
    sign_payment,
)

HOSTED = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway"
PAY_TO = "0x" + "9" * 40


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOSTED / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mods():
    for name in ("workspaces", "stripe_checkout", "chain_check", "report"):
        _load(name, f"{name}.py")
    return _load("budget_gateway_service", "service.py")


class SlowFacilitator(FakeFacilitator):
    """Yields to the event loop inside verify and settle, so 30 purchases really interleave."""

    async def verify(self, payload, requirements):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.01)
        return await super().verify(payload, requirements)

    async def settle(self, payload, requirements):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.01)
        return await super().settle(payload, requirements)


def test_a_burst_of_purchases_cannot_exceed_the_launch_caps(mods, tmp_path):
    fac = SlowFacilitator()
    settings = mods.Settings.from_env(
        {
            "BG_DATA_DIR": str(tmp_path / "data"),
            "BG_X402_PAY_TO": PAY_TO,
            "BG_MAX_DAILY_SALES_USD": "25",
            "BG_MAX_OUTSTANDING_CREDITS_USD": "50",
        }
    )
    app = mods.create_app(settings, facilitator=fac)

    async def burst() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hosted") as c:
            keys = [(await c.post("/v1/workspaces")).json()["api_key"] for _ in range(3)]
            auths = [{"Authorization": f"Bearer {keys[i % 3]}"} for i in range(30)]
            unpaid = await c.post("/v1/credits/x402", headers=auths[0])
            required = decode_payment_required(unpaid)
            signed = [sign_payment(required) for _ in range(30)]  # 30 distinct buyers

            async def pay(i: int) -> int:
                headers = {**auths[i], "PAYMENT-SIGNATURE": signed[i].header}
                return (await c.post("/v1/credits/x402", headers=headers)).status_code

            return list(await asyncio.gather(*(pay(i) for i in range(30))))

    # Creating 3 workspaces from one address is within the per-address creation limit (5/hour).
    codes = asyncio.run(burst())
    assert sorted(codes) == [200] * 25 + [503] * 5
    assert len(fac.calls_to("settle")) == 25  # the 5 refused were never settled
    ledger = app.state.ledger
    assert ledger.sales_cents_since(0) == 2500
    assert ledger.outstanding_liability_cents() == 2500
    assert ledger.pending() == []
