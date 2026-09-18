"""Tests for the usage/presence beacon
(docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md,
superseding docs/adr/0019's "opt-in, default off"): install id
persistence, the mutable enabled/sent-events state file, the exact
payload shape, the fire-and-forget client (never blocks, never raises,
fires each once-ever event at most once, heartbeats at most once per 24h),
and the `ReceiptSink` wrapper that drives `first_receipt` off real receipt
traffic.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from inferrail.config.models import InferrailConfig, UsagePingConfig
from inferrail.gateway import app as app_module
from inferrail.receipts.schema import InferenceReceipt
from inferrail.usage_ping import client as ping_client
from inferrail.usage_ping import state as ping_state
from inferrail.usage_ping.install_id import ensure_install_id
from inferrail.usage_ping.payload import KNOWN_EVENTS, build_payload
from inferrail.usage_ping.receipt_hook import UsagePingReceiptSink


def _wait_until(predicate: object, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.02)
    raise AssertionError("condition never became true within timeout")


def _receipt(status: str = "success") -> InferenceReceipt:
    return InferenceReceipt(
        receipt_id="r1",
        request_id="req1",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status=status,  # type: ignore[arg-type]
        total_latency_ms=12.5,
    )


class _InnerSink:
    def __init__(self) -> None:
        self.emitted: list[InferenceReceipt] = []

    def emit(self, receipt: InferenceReceipt) -> None:
        self.emitted.append(receipt)


@pytest.fixture(autouse=True)
def _not_treated_as_running_under_pytest(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test in this file except the two marked `real_env_gate`
    deliberately calls the usage-ping client directly, so the real
    `PYTEST_CURRENT_TEST`-based test-suite gate
    (`usage_ping.client._disabled_by_environment`) would otherwise make
    every single one of them a silent no-op. `monkeypatch.delenv` doesn't
    work here -- pytest itself rewrites `PYTEST_CURRENT_TEST` at each
    phase transition (setup/call/teardown), overwriting a deletion made
    during setup before the test body ever runs -- so this patches the
    gate function directly instead, except for the two tests that exist
    specifically to cover the real, unpatched guard."""
    if request.node.get_closest_marker("real_env_gate") is None:
        monkeypatch.setattr(ping_client, "_disabled_by_environment", lambda: False)


# --- install_id -------------------------------------------------------


def test_install_id_persists_and_is_stable(tmp_path: Path) -> None:
    first = ensure_install_id(tmp_path)
    second = ensure_install_id(tmp_path)
    assert first == second
    assert len(first) >= 16


def test_install_id_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "dir"
    install_id = ensure_install_id(nested)
    assert (nested / "usage-ping-install-id").exists()
    assert ensure_install_id(nested) == install_id


# --- state --------------------------------------------------------------


def test_load_state_seeds_from_config_default_on_first_use(tmp_path: Path) -> None:
    state = ping_state.load_state(tmp_path, default_enabled=True)
    assert state.enabled is True
    # Persisted, not just returned:
    assert ping_state.load_state(tmp_path, default_enabled=False).enabled is True


def test_state_defaults_to_enabled_opt_out(tmp_path: Path) -> None:
    # ADR-0020: opt-out by default, unlike ADR-0019's original "off".
    state = ping_state.load_state(tmp_path)
    assert state.enabled is True


def test_save_state_round_trips(tmp_path: Path) -> None:
    state = ping_state.load_state(tmp_path)
    state.enabled = True
    ping_state.save_state(tmp_path, state)
    assert ping_state.load_state(tmp_path).enabled is True


def test_corrupted_state_file_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "usage-ping-state.json").write_text("not json", encoding="utf-8")
    state = ping_state.load_state(tmp_path, default_enabled=True)
    assert state.enabled is True  # falls back to the given default, not an exception


def test_mark_event_sent_if_new_fires_once(tmp_path: Path) -> None:
    assert ping_state.mark_event_sent_if_new(tmp_path, "install", ts="t1") is True
    assert ping_state.mark_event_sent_if_new(tmp_path, "install", ts="t2") is False
    # A different event is independent:
    assert ping_state.mark_event_sent_if_new(tmp_path, "first_receipt", ts="t3") is True


def test_mark_heartbeat_sent_if_due_first_time_and_then_gated(tmp_path: Path) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    assert ping_state.mark_heartbeat_sent_if_due(tmp_path, now=t0) is True

    # Too soon -- less than the minimum interval later.
    soon = t0 + timedelta(hours=1)
    assert ping_state.mark_heartbeat_sent_if_due(tmp_path, now=soon) is False

    # Far enough later -- due again.
    later = t0 + timedelta(hours=25)
    assert ping_state.mark_heartbeat_sent_if_due(tmp_path, now=later) is True


# --- payload --------------------------------------------------------------


def test_build_payload_has_exactly_the_allowed_fields() -> None:
    payload = build_payload("install", "install-123")
    assert set(payload) == {"install_id", "event", "version", "os", "python_version"}
    assert payload["install_id"] == "install-123"
    assert payload["event"] == "install"


def test_build_payload_python_version_is_major_minor_only() -> None:
    payload = build_payload("install", "install-123")
    parts = payload["python_version"].split(".")
    assert len(parts) == 2
    assert all(p.isdigit() for p in parts)


def test_build_payload_rejects_unknown_event() -> None:
    with pytest.raises(ValueError, match="unknown usage-ping event"):
        build_payload("not_a_real_event", "install-123")


def test_all_known_events_build_a_valid_payload() -> None:
    assert set(KNOWN_EVENTS) == {"install", "serve_start", "first_receipt", "heartbeat"}
    for event in KNOWN_EVENTS:
        payload = build_payload(event, "install-123")
        assert payload["event"] == event


# --- client ---------------------------------------------------------------


def test_no_endpoint_configured_never_touches_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append("called"))

    ping_client.maybe_send_event(
        "install", app_data_dir=tmp_path, config=UsagePingConfig(enabled=True, endpoint=None)
    )
    time.sleep(0.1)
    assert calls == []
    # Not even the local marker should be touched -- endpoint is checked first.
    assert not (tmp_path / "usage-ping-state.json").exists()


def test_disabled_never_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append("called"))

    ping_client.maybe_send_event(
        "install",
        app_data_dir=tmp_path,
        config=UsagePingConfig(enabled=False, endpoint="http://example.invalid/ping"),
    )
    time.sleep(0.1)
    assert calls == []


@pytest.mark.real_env_gate
@pytest.mark.parametrize("env_var,value", [("INFERRAIL_TELEMETRY", "0"), ("DO_NOT_TRACK", "1")])
def test_environment_opt_out_overrides_an_enabled_configured_ping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_var: str, value: str
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append("called"))
    monkeypatch.setenv(env_var, value)
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")

    ping_client.maybe_send_event("install", app_data_dir=tmp_path, config=config)
    time.sleep(0.1)
    assert calls == []


@pytest.mark.real_env_gate
def test_ci_environment_disables_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append("called"))
    monkeypatch.setenv("CI", "true")
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")

    ping_client.maybe_send_event("install", app_data_dir=tmp_path, config=config)
    time.sleep(0.1)
    assert calls == []


@pytest.mark.real_env_gate
def test_running_under_pytest_disables_sending_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unlike every other test in this file, exercises the real,
    # unpatched `_disabled_by_environment` gate that protects the rest of
    # this project's test suite from ever sending a real beacon, without
    # that guard needing a special fixture to kick in.
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append("called"))
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")

    ping_client.maybe_send_event("install", app_data_dir=tmp_path, config=config)
    time.sleep(0.1)
    assert calls == []


def test_enabled_and_configured_sends_once_per_once_ever_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[dict[str, object]] = []

    def _fake_post(url: str, *, json: dict[str, object], timeout: float) -> None:
        received.append({"url": url, "json": json})

    monkeypatch.setattr(httpx, "post", _fake_post)
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")

    ping_client.maybe_send_event("install", app_data_dir=tmp_path, config=config)
    _wait_until(lambda: len(received) == 1)
    assert received[0]["url"] == "http://example.invalid/ping"
    assert received[0]["json"]["event"] == "install"  # type: ignore[index]

    # Firing the same event again must not send a second time.
    ping_client.maybe_send_event("install", app_data_dir=tmp_path, config=config)
    time.sleep(0.1)
    assert len(received) == 1

    # A different once-ever event does send.
    ping_client.maybe_send_event("first_receipt", app_data_dir=tmp_path, config=config)
    _wait_until(lambda: len(received) == 2)


def test_serve_start_is_never_deduped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")

    ping_client.maybe_send_event("serve_start", app_data_dir=tmp_path, config=config)
    ping_client.maybe_send_event("serve_start", app_data_dir=tmp_path, config=config)
    _wait_until(lambda: received == ["serve_start", "serve_start"])


def test_send_failure_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **kw: object) -> None:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", _raise)
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")

    # Must return immediately, without raising, even though the
    # background thread's own send will fail.
    ping_client.maybe_send_event("install", app_data_dir=tmp_path, config=config)
    # Give the background thread a moment to actually hit (and swallow)
    # the failure, so a regression that lets it propagate would surface
    # as an unhandled-thread-exception warning, not silence.
    time.sleep(0.2)


# --- heartbeat --------------------------------------------------------


def test_maybe_send_heartbeat_respects_the_24h_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    ping_client.maybe_send_heartbeat(app_data_dir=tmp_path, config=config, now=t0)
    _wait_until(lambda: received == ["heartbeat"])

    ping_client.maybe_send_heartbeat(
        app_data_dir=tmp_path, config=config, now=t0 + timedelta(hours=1)
    )
    time.sleep(0.1)
    assert received == ["heartbeat"]  # still just the one -- too soon

    ping_client.maybe_send_heartbeat(
        app_data_dir=tmp_path, config=config, now=t0 + timedelta(hours=25)
    )
    _wait_until(lambda: received == ["heartbeat", "heartbeat"])


def test_start_heartbeat_thread_does_not_start_without_an_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    before = {t.name for t in threading.enumerate()}
    ping_client.start_heartbeat_thread(
        app_data_dir=tmp_path, config=UsagePingConfig(enabled=True, endpoint=None)
    )
    time.sleep(0.05)
    after = {t.name for t in threading.enumerate()}
    assert after - before == set()


@pytest.mark.real_env_gate
def test_start_heartbeat_thread_does_not_start_under_pytest(tmp_path: Path) -> None:
    # The real, unpatched guard should prevent the thread from ever being
    # created, not just from ever sending.
    import threading

    before = {t.name for t in threading.enumerate()}
    ping_client.start_heartbeat_thread(
        app_data_dir=tmp_path,
        config=UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping"),
    )
    time.sleep(0.05)
    after = {t.name for t in threading.enumerate()}
    assert after - before == set()


# --- receipt_hook -----------------------------------------------------


def test_wrapper_forwards_emit_unchanged(tmp_path: Path) -> None:
    inner = _InnerSink()
    sink = UsagePingReceiptSink(inner, app_data_dir=tmp_path, config=UsagePingConfig())
    receipt = _receipt()
    sink.emit(receipt)
    assert inner.emitted == [receipt]


def test_wrapper_fires_first_receipt_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")
    sink = UsagePingReceiptSink(_InnerSink(), app_data_dir=tmp_path, config=config)

    sink.emit(_receipt(status="success"))
    _wait_until(lambda: received == ["first_receipt"])


def test_wrapper_error_receipt_also_fires_first_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")
    sink = UsagePingReceiptSink(_InnerSink(), app_data_dir=tmp_path, config=config)

    sink.emit(_receipt(status="error"))
    _wait_until(lambda: received == ["first_receipt"])


def test_wrapper_second_receipt_never_refires_first_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")
    sink = UsagePingReceiptSink(_InnerSink(), app_data_dir=tmp_path, config=config)

    sink.emit(_receipt(status="success"))
    _wait_until(lambda: received == ["first_receipt"])
    sink.emit(_receipt(status="success"))
    time.sleep(0.1)
    assert received == ["first_receipt"]  # no duplicate


# --- create_app integration --------------------------------------------


def _serve_config(
    tmp_path: Path, *, usage_ping: UsagePingConfig, app_mode: bool
) -> InferrailConfig:
    # Usage-ping's storage location follows `budgets.path`'s parent
    # (see gateway/app.py) regardless of whether budgets are actually
    # enabled -- set explicitly here, in both branches, so these tests
    # stay isolated to `tmp_path` rather than touching this process's
    # real cwd or OS app-data directory.
    if app_mode:
        return InferrailConfig.model_validate(
            {
                "providers": {"openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"}},
                "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
                "telemetry": {"sink": "none"},
                "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
                "budgets": {"enabled": True, "path": str(tmp_path / "budgets.db")},
                "usage_ping": usage_ping,
            }
        )
    return InferrailConfig.model_validate(
        {
            "providers": {"openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"}},
            "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "jsonl", "path": str(tmp_path / "r.jsonl")},
            "budgets": {"enabled": False, "path": str(tmp_path / "budgets.db")},
            "usage_ping": usage_ping,
        }
    )


def test_serve_startup_fires_install_and_serve_start_once_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    config = _serve_config(
        tmp_path,
        usage_ping=UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping"),
        app_mode=True,
    )

    app_module.create_app(config, app_mode=True, local_outcomes_path=tmp_path / "o.jsonl")
    _wait_until(lambda: set(received) == {"install", "serve_start"})

    # A second create_app (a second `inferrail serve` process) refires
    # serve_start but never install again.
    received.clear()
    app_module.create_app(config, app_mode=True, local_outcomes_path=tmp_path / "o.jsonl")
    _wait_until(lambda: received == ["serve_start"])
    time.sleep(0.1)
    assert received == ["serve_start"]


def test_serve_startup_fires_regardless_of_app_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0020 reverses ADR-0019's app-mode-only scoping -- a plain
    # `inferrail serve` (no --app-mode, no --quickstart) now fires too.
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    config = _serve_config(
        tmp_path,
        usage_ping=UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping"),
        app_mode=False,
    )

    app_module.create_app(config, app_mode=False)

    _wait_until(lambda: set(received) == {"install", "serve_start"})


def test_no_endpoint_means_serve_never_touches_usage_ping_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    config = _serve_config(
        tmp_path, usage_ping=UsagePingConfig(enabled=True, endpoint=None), app_mode=False
    )

    app_module.create_app(config, app_mode=False)
    time.sleep(0.2)

    assert received == []
    assert not (tmp_path / "usage-ping-state.json").exists()
