"""The production host installs Inferrail Hosted from requirements files, while CI's Hosted job
installs the `hosted` extra. The x402 pin and extras must match, or a dependency CI has can be
missing in production (as `extensions`/jsonschema was, which crashed startup with payments on)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PIN = re.compile(r"^x402\[([^\]]*)\]==(\S+)", re.M)


def _x402(text: str) -> tuple[set[str], str]:
    m = PIN.search(text)
    assert m, "no pinned x402 requirement"
    return {e.strip() for e in m.group(1).split(",")}, m.group(2)


def test_production_requirements_carry_the_same_x402_as_the_hosted_extra():
    hosted = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]["hosted"]
    extra_extras, extra_version = _x402("\n".join(hosted))
    prod_extras, prod_version = _x402(
        (ROOT / "hosted" / "budget_gateway" / "requirements.txt").read_text()
    )
    assert prod_version == extra_version
    assert "extensions" in prod_extras  # the credit route's Bazaar metadata needs it
    assert extra_extras <= prod_extras, extra_extras - prod_extras
