"""The pre-deploy payment check stops a deploy that would crash the shared service at startup or
collect payments differently from what was approved, and never prints secret values."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

HOSTED = Path(__file__).resolve().parents[3] / "hosted" / "budget_gateway"
APPROVED = "0x5a5A5a5a5A5a5a5a5a5A5a5A5A5a5a5A5A5A5A5A"
SECRET = "very-secret-api-key-value"


@pytest.fixture
def preflight(tmp_path):
    sys.path.insert(0, str(HOSTED))
    spec = importlib.util.spec_from_file_location("hosted_preflight", HOSTED / "preflight.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(tmp_path, **extra):
    return {"BG_DATA_DIR": str(tmp_path / "data"), **extra}


def _mainnet(tmp_path, **extra):
    base = {
        "BG_X402_PAY_TO": APPROVED,
        "BG_X402_NETWORK": "eip155:8453",
        "BG_X402_MAINNET_APPROVED": "1",
        "BG_BASE_RPC_URL": "https://rpc.example",
        "CDP_API_KEY_ID": "key-id",
        "CDP_API_KEY_SECRET": SECRET,
    }
    return _env(tmp_path, **{**base, **extra})


def _run(preflight, env, expect=APPROVED, chain="0x2105", kinds=None):
    return preflight.check(
        env,
        expect,
        chain_id=lambda url: chain,
        kinds=lambda e: kinds if kinds is not None else {("exact", "eip155:8453")},
        build=lambda settings: None,
    )


def test_payments_off_passes_without_an_expected_address(preflight, tmp_path, capsys):
    assert _run(preflight, _env(tmp_path), expect=None) == []
    assert "payments: OFF" in capsys.readouterr().out


def test_the_approved_mainnet_configuration_passes(preflight, tmp_path, capsys):
    assert _run(preflight, _mainnet(tmp_path)) == []
    out = capsys.readouterr().out
    assert APPROVED in out and "chain id 0x2105" in out and "exact on eip155:8453" in out
    assert SECRET not in out


@pytest.mark.parametrize(
    ("drop", "set_", "needle"),
    [
        ("BG_X402_MAINNET_APPROVED", {}, "would not start"),
        ("BG_BASE_RPC_URL", {}, "would not start"),
        ("CDP_API_KEY_SECRET", {}, "mainnet needs CDP_API_KEY_SECRET"),
        (None, {"BG_X402_PAY_TO": "0x" + "1" * 40}, "not the approved"),
        (None, {"CDP_WALLET_SECRET": SECRET}, "CDP_WALLET_SECRET is set"),
        (None, {"BG_X402_NETWORK": "eip155:1"}, "would not start"),
        (None, {"BG_MOUNT_PREFIX": "/elsewhere"}, "would not start"),
    ],
)
def test_unsafe_configurations_stop_the_deploy(preflight, tmp_path, drop, set_, needle):
    env = _mainnet(tmp_path, **set_)
    if drop:
        env.pop(drop)
    problems = _run(preflight, env)
    assert any(needle in p for p in problems), problems
    assert all(SECRET not in p for p in problems)


def test_wrong_chain_or_unsupported_facilitator_stops_the_deploy(preflight, tmp_path):
    assert any("not eip155:8453" in p for p in _run(preflight, _mainnet(tmp_path), chain="0x1"))
    assert any(
        "does not support exact" in p
        for p in _run(preflight, _mainnet(tmp_path), kinds={("exact", "eip155:84532")})
    )


def test_payments_on_without_an_expected_address_stops_the_deploy(preflight, tmp_path):
    assert any("no --expect-pay-to" in p for p in _run(preflight, _mainnet(tmp_path), expect=None))


def test_network_failures_stop_the_deploy_without_leaking(preflight, tmp_path):
    def boom(*_a):
        raise ConnectionError(SECRET)

    problems = preflight.check(
        _mainnet(tmp_path), APPROVED, chain_id=boom, kinds=boom, build=lambda settings: None
    )
    assert len(problems) == 2 and all(SECRET not in p for p in problems)


def test_the_real_app_is_built_with_payments_off_and_on(preflight, tmp_path, capsys):
    """No stubbed builder: the same combined app the service starts, on a throwaway directory.
    "On" uses Base Sepolia (no facilitator key needed) and a burn address."""
    pytest.importorskip("x402")
    assert preflight.check(_env(tmp_path), None) == []
    on = _env(tmp_path, BG_X402_PAY_TO="0x000000000000000000000000000000000000dEaD")
    assert preflight.check(on, "0x000000000000000000000000000000000000dEaD") == []
    assert capsys.readouterr().out.count("the application builds") == 2


def test_a_startup_import_error_stops_the_deploy(preflight, tmp_path):
    """The production failure this guards against: an import that only runs when the credit
    rail is on, missing from the production install."""

    def missing_extra(settings):
        raise ImportError(
            "Extensions validation requires jsonschema. Install with: pip install x402[extensions]"
        )

    problems = preflight.check(
        _mainnet(tmp_path),
        APPROVED,
        chain_id=lambda u: "0x2105",
        kinds=lambda e: set(),
        build=missing_extra,
    )
    assert problems == [
        "the application would not start: ImportError: Extensions validation requires "
        "jsonschema. Install with: pip install x402[extensions]"
    ]
