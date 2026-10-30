"""The README's Web Read facts come from the service, so a price or route change can't leave
them stale."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_readme_states_web_reads_price_route_and_recovery_window():
    svc = _load("web_read_service_docs", ROOT / "hosted" / "web_read" / "service.py")
    cohost = _load("budget_gateway_cohost_docs", ROOT / "hosted" / "budget_gateway" / "cohost.py")
    readme = (ROOT / "README.md").read_text()
    section = readme.split("## For agents: Web Read", 1)[1].split("\n## ", 1)[0]
    text = " ".join(section.split())  # ignore line wrapping
    defaults = svc.Settings(pay_to="0x" + "0" * 40)
    assert f"${defaults.price_usd} per delivered page" in text
    assert f"{cohost.READ_PREFIX}/v1/page" in text
    assert f"within {defaults.recovery_s // 60} minutes" in text


def test_the_site_llms_txt_states_the_live_prices_and_allowance():
    svc = _load("web_read_service_llms", ROOT / "hosted" / "web_read" / "service.py")
    hosted = _load("budget_gateway_service_llms", ROOT / "hosted" / "budget_gateway" / "service.py")
    text = " ".join((ROOT / "docs" / "llms.txt").read_text().split())
    defaults = svc.Settings(pay_to="0x" + "0" * 40)
    free_runs = hosted.Settings.__dataclass_fields__["free_runs_per_month"].default
    assert f"${defaults.price_usd} per delivered page" in text
    assert f"within {defaults.recovery_s // 60} minutes" in text
    assert f"{free_runs} runs a month free" in text
