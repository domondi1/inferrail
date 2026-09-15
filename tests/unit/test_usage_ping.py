"""Tests for the opt-in usage ping (docs/adr/0019-opt-in-usage-ping.md):
install id persistence, the mutable enabled/sent-events state file, the
exact payload shape, the fire-and-forget client (never blocks, never
raises, fires each event at most once), and the `ReceiptSink` wrapper
that drives `first_receipt`/`tool_connected` off real receipt traffic.
"""

from __future__ import annotations

import time
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
    assert ping_state.mark_event_sent_if_new(tmp_path, "first_run", ts="t1") is True
    assert ping_state.mark_event_sent_if_new(tmp_path, "first_run", ts="t2") is False
    # A different event is independent:
    assert ping_state.mark_event_sent_if_new(tmp_path, "budget_created", ts="t3") is True


# --- payload --------------------------------------------------------------


def test_build_payload_has_exactly_the_allowed_fields() -> None:
    payload = build_payload("first_run", "install-123")
    assert set(payload) == {"install_id", "event", "os", "inferrail_version", "ts"}
    assert payload["install_id"] == "install-123"
    assert payload["event"] == "first_run"


def test_build_payload_rejects_unknown_event() -> None:
    with pytest.raises(ValueError, match="unknown usage-ping event"):
        build_payload("not_a_real_event", "install-123")


def test_all_known_events_build_a_valid_payload() -> None:
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
        "first_run", app_data_dir=tmp_path, config=UsagePingConfig(enabled=True, endpoint=None)
    )
    time.sleep(0.1)
    assert calls == []
    # Not even the local marker should be touched -- endpoint is checked first.
    assert not (tmp_path / "usage-ping-state.json").exists()


def test_disabled_never_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append("called"))

    ping_client.maybe_send_event(
        "first_run",
        app_data_dir=tmp_path,
        config=UsagePingConfig(enabled=False, endpoint="http://example.invalid/ping"),
    )
    time.sleep(0.1)
    assert calls == []


def test_enabled_and_configured_sends_once_per_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[dict[str, object]] = []

    def _fake_post(url: str, *, json: dict[str, object], timeout: float) -> None:
        received.append({"url": url, "json": json})

    monkeypatch.setattr(httpx, "post", _fake_post)
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")

    ping_client.maybe_send_event("first_run", app_data_dir=tmp_path, config=config)
    _wait_until(lambda: len(received) == 1)
    assert received[0]["url"] == "http://example.invalid/ping"
    assert received[0]["json"]["event"] == "first_run"  # type: ignore[index]

    # Firing the same event again must not send a second time.
    ping_client.maybe_send_event("first_run", app_data_dir=tmp_path, config=config)
    time.sleep(0.1)
    assert len(received) == 1

    # A different event does send.
    ping_client.maybe_send_event("budget_created", app_data_dir=tmp_path, config=config)
    _wait_until(lambda: len(received) == 2)


def test_send_failure_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **kw: object) -> None:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", _raise)
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")

    # Must return immediately, without raising, even though the
    # background thread's own send will fail.
    ping_client.maybe_send_event("first_run", app_data_dir=tmp_path, config=config)
    # Give the background thread a moment to actually hit (and swallow)
    # the failure, so a regression that lets it propagate would surface
    # as an unhandled-thread-exception warning, not silence.
    time.sleep(0.2)


# --- receipt_hook -----------------------------------------------------


def test_wrapper_forwards_emit_unchanged(tmp_path: Path) -> None:
    inner = _InnerSink()
    sink = UsagePingReceiptSink(inner, app_data_dir=tmp_path, config=UsagePingConfig())
    receipt = _receipt()
    sink.emit(receipt)
    assert inner.emitted == [receipt]


def test_wrapper_fires_first_receipt_and_tool_connected_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")
    sink = UsagePingReceiptSink(_InnerSink(), app_data_dir=tmp_path, config=config)

    sink.emit(_receipt(status="success"))
    _wait_until(lambda: len(received) == 2)
    assert set(received) == {"first_receipt", "tool_connected"}


def test_wrapper_error_receipt_fires_first_receipt_but_not_tool_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    config = UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")
    sink = UsagePingReceiptSink(_InnerSink(), app_data_dir=tmp_path, config=config)

    sink.emit(_receipt(status="error"))
    _wait_until(lambda: len(received) == 1)
    time.sleep(0.1)
    assert received == ["first_receipt"]


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
    _wait_until(lambda: len(received) == 2)
    sink.emit(_receipt(status="success"))
    time.sleep(0.1)
    assert len(received) == 2  # no duplicate first_receipt/tool_connected


# --- create_app integration --------------------------------------------


def _app_mode_config(tmp_path: Path, *, usage_ping: UsagePingConfig) -> InferrailConfig:
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


def test_app_mode_startup_fires_first_run_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    config = _app_mode_config(
        tmp_path, usage_ping=UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping")
    )

    app_module.create_app(config, app_mode=True, local_outcomes_path=tmp_path / "o.jsonl")
    _wait_until(lambda: received == ["first_run"])

    # A second create_app against the same app-data dir must not refire it.
    app_module.create_app(config, app_mode=True, local_outcomes_path=tmp_path / "o.jsonl")
    time.sleep(0.1)
    assert received == ["first_run"]


def test_non_app_mode_never_touches_usage_ping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        httpx, "post", lambda url, *, json, timeout: received.append(json["event"])
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    config = InferrailConfig.model_validate(
        {
            "providers": {"openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"}},
            "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "jsonl", "path": str(tmp_path / "r.jsonl")},
            "usage_ping": UsagePingConfig(enabled=True, endpoint="http://example.invalid/ping"),
        }
    )

    app_module.create_app(config, app_mode=False)
    time.sleep(0.2)

    assert received == []
    assert not (tmp_path / "usage-ping-state.json").exists()
